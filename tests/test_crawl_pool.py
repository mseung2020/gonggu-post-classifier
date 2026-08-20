"""crawl_pool.py — 공용 워커 풀(2단계 B3)의 계약: 전 항목 정확히 1회 처리, 워커당 자원
setup/teardown, 예외에도 워커 생존, 세션 저장은 워커 0번만. 실제 Playwright 없이
LazyPage/sync_playwright를 페이크로 바꿔 검증한다."""
import threading
import time

import gonggu.crawl_pool as cp


class _FakePage:
    def __init__(self, pw, save_auth_state=False):
        self.save_auth_state = save_auth_state
        self.released = 0
        self.closed = False
        self.used_since_release = False  # 실제 LazyPage는 페이지 접근 시 True가 됨

    def release_if_contended(self):
        self.released += 1
        self.used_since_release = False

    def close(self):
        self.closed = True


class _FakePlaywrightCtx:
    def __enter__(self):
        return object()

    def __exit__(self, *a):
        return False


def _patch(monkeypatch, pages):
    monkeypatch.setattr(cp, 'sync_playwright', lambda: _FakePlaywrightCtx())

    def make_page(pw, save_auth_state=False):
        page = _FakePage(pw, save_auth_state)
        pages.append(page)
        return page

    monkeypatch.setattr(cp, 'LazyPage', make_page)


class TestRunCrawlPool:
    def test_every_item_handled_once(self, monkeypatch):
        pages = []
        _patch(monkeypatch, pages)
        seen = []
        lock = threading.Lock()

        def handle(ctx, item):
            with lock:
                seen.append(item)

        n = cp.run_crawl_pool(list(range(20)), handle, concurrency=4)
        assert n == 4
        assert sorted(seen) == list(range(20))
        assert all(p.closed for p in pages)
        assert sum(p.released for p in pages) == 20  # 항목마다 정확히 1번 release_if_contended

    def test_workers_capped_by_items(self, monkeypatch):
        pages = []
        _patch(monkeypatch, pages)
        assert cp.run_crawl_pool([1, 2], lambda c, i: None, concurrency=10) == 2
        assert cp.run_crawl_pool([], lambda c, i: None, concurrency=10) == 1  # 기존 min(..., len or 1)

    def test_exception_does_not_kill_worker(self, monkeypatch, capsys):
        """예전 rescan 사고(예외로 스레드가 조용히 죽어 남은 물량 방치)의 재발 방지 계약."""
        pages = []
        _patch(monkeypatch, pages)
        seen = []
        lock = threading.Lock()

        def handle(ctx, item):
            if item == 0:
                raise RuntimeError('boom')
            with lock:
                seen.append(item)

        cp.run_crawl_pool([0, 1, 2, 3], handle, concurrency=1)  # 워커 1개 — 죽으면 나머지 전부 방치됨
        assert sorted(seen) == [1, 2, 3]
        assert '건너뜀' in capsys.readouterr().out

    def test_setup_teardown_per_worker_and_auth_only_first(self, monkeypatch):
        pages = []
        _patch(monkeypatch, pages)
        made, closed = [], []

        def setup():
            made.append(1)
            return {'conn': len(made)}

        cp.run_crawl_pool(list(range(6)), lambda c, i: None, concurrency=3,
                          worker_setup=setup, worker_teardown=lambda s: closed.append(s['conn']))
        assert len(made) == 3 and sorted(closed) == [1, 2, 3]
        assert sum(1 for p in pages if p.save_auth_state) == 1  # 워커 0번만

    def test_save_auth_disabled(self, monkeypatch):
        pages = []
        _patch(monkeypatch, pages)
        cp.run_crawl_pool([1, 2, 3], lambda c, i: None, concurrency=2, save_auth_first_worker=False)
        assert all(not p.save_auth_state for p in pages)

    def test_warn_hint_printed(self, monkeypatch, capsys):
        pages = []
        _patch(monkeypatch, pages)
        monkeypatch.setattr(cp, 'MAX_BROWSERS', 1)
        cp.run_crawl_pool(list(range(10)), lambda c, i: None, concurrency=10, warn_hint='X_CONCURRENCY')
        out = capsys.readouterr().out
        assert 'X_CONCURRENCY를 낮춰보세요' in out


class TestConditionalDelay:
    """4단계 D1 — delay_only_after_browser: 브라우저를 쓴 항목만 item_delay를 적용."""

    def _run(self, monkeypatch, *, smart, browser_items):
        pages = []
        _patch(monkeypatch, pages)
        sleeps = []
        monkeypatch.setattr(cp.time, 'sleep', sleeps.append)

        def handle(ctx, item):
            if item in browser_items:
                ctx.page.used_since_release = True  # 이 항목에서 브라우저를 썼다고 표시

        cp.run_crawl_pool([0, 1, 2, 3], handle, concurrency=1, item_delay=3.0,
                          delay_only_after_browser=smart)
        return sleeps

    def test_smart_skips_fastpath_items(self, monkeypatch):
        sleeps = self._run(monkeypatch, smart=True, browser_items={1, 3})
        assert sleeps == [3.0, 3.0]  # 브라우저 쓴 2개 항목만 대기

    def test_legacy_sleeps_every_item(self, monkeypatch):
        sleeps = self._run(monkeypatch, smart=False, browser_items={1})
        assert sleeps == [3.0] * 4

    def test_smart_with_no_browser_never_sleeps(self, monkeypatch):
        assert self._run(monkeypatch, smart=True, browser_items=set()) == []


class TestStallWatchdog:
    """스톨 워치독(2026-08-12) — 죽은 Playwright 드라이버에 워커가 물려 풀 전체가 무한 정지할 때,
    CRAWL_STALL_TIMEOUT초 무진척이면 이 단계를 강제 종료(os._exit)해 몇 시간짜리 침묵 정지를
    '빨리 티나게 실패 → --from 재개'로 바꾼다."""

    def test_should_abort_truth_table(self):
        assert cp._should_abort(idle=100, done=5, total=10, timeout=300) is False   # idle<timeout
        assert cp._should_abort(idle=301, done=5, total=10, timeout=300) is True    # 무진척 초과
        assert cp._should_abort(idle=301, done=10, total=10, timeout=300) is False  # 다 끝남
        assert cp._should_abort(idle=99999, done=5, total=10, timeout=0) is False   # 0이면 꺼짐

    def test_stall_message_has_resume_hint(self):
        msg = cp._stall_message(400, 1037, 3083, warn_hint='RESCAN_CONCURRENCY')
        assert '--from' in msg and '1037/3083' in msg and 'RESCAN_CONCURRENCY' in msg
        assert '--from' in cp._stall_message(400, 1, 2)          # 힌트 없어도 재개 안내는 항상

    def test_normal_run_does_not_abort(self, monkeypatch):
        """진척이 계속되면 워치독은 발동하지 않는다(오발동 방지)."""
        pages = []
        _patch(monkeypatch, pages)
        monkeypatch.setattr(cp, 'STALL_TIMEOUT', 5.0)
        aborted = []
        monkeypatch.setattr(cp, '_abort', lambda msg: aborted.append(msg))
        seen = []
        lock = threading.Lock()
        cp.run_crawl_pool(list(range(20)), lambda c, i: (lock.acquire(), seen.append(i), lock.release()),
                          concurrency=4)
        assert sorted(seen) == list(range(20)) and aborted == []

    def test_watchdog_fires_when_fully_stalled(self, monkeypatch):
        """전 워커가 한 항목에서 멈춰 진척이 0이면 _abort가 호출된다."""
        pages = []
        _patch(monkeypatch, pages)
        monkeypatch.setattr(cp, 'STALL_TIMEOUT', 0.4)
        fired = threading.Event()
        release = threading.Event()
        monkeypatch.setattr(cp, '_abort', lambda msg: fired.set())   # os._exit 대신 플래그만

        def handle(ctx, item):
            release.wait(10)     # 전 워커가 여기서 멈춤 → done이 0에서 안 늘어남

        done = threading.Event()
        threading.Thread(target=lambda: (cp.run_crawl_pool([1, 2, 3, 4], handle, concurrency=4),
                                         done.set()), daemon=True).start()
        assert fired.wait(3), '스톨 임계(0.4s)를 넘겼는데 워치독이 안 울렸다'
        release.set()            # 워커 풀어줘 정상 종료(테스트 프로세스는 _abort를 가짜로 둬 안 죽음)
        assert done.wait(5)


class TestRecycleWatchdog:
    """정기 재기동(2026-08-18) — 사용자가 직접 관찰한 "브라우저 풀을 오래 재사용할수록 느려지고,
    껐다 켜면 다시 빨라진다"는 문제를 스톨과 별개로 자동화한다. CRAWL_RECYCLE_SEC가 지나면
    스톨이 아니어도(진척은 계속되고 있어도) 건강한 재시작으로 프로세스를 끝낸다."""

    def test_should_recycle_truth_table(self):
        assert cp._should_recycle(elapsed=100, done=5, total=10, recycle_after=240) is False  # 아직 안 지남
        assert cp._should_recycle(elapsed=241, done=5, total=10, recycle_after=240) is True    # 지남, 남은 일 있음
        assert cp._should_recycle(elapsed=241, done=10, total=10, recycle_after=240) is False  # 다 끝남 — 재기동 불필요
        assert cp._should_recycle(elapsed=99999, done=5, total=10, recycle_after=0) is False   # 0이면 꺼짐

    def test_recycle_message_has_resume_hint_and_is_not_a_failure_tone(self):
        msg = cp._recycle_message(250, 1200, 5000)
        assert '--from' in msg and '1200/5000' in msg
        assert '먹통' not in msg  # 스톨 메시지와 달리 "드라이버 먹통"이 아니라 의도된 재시작임을 명확히 함
        assert '의도된' in msg

    def test_normal_run_does_not_recycle_when_disabled(self, monkeypatch):
        """RECYCLE_AFTER_SEC=0(기본, 꺼짐)이면 워치독이 재기동을 발동하지 않는다."""
        pages = []
        _patch(monkeypatch, pages)
        monkeypatch.setattr(cp, 'RECYCLE_AFTER_SEC', 0.0)
        recycled = []
        monkeypatch.setattr(cp, '_recycle', lambda msg: recycled.append(msg))
        seen = []
        lock = threading.Lock()
        cp.run_crawl_pool(list(range(20)), lambda c, i: (lock.acquire(), seen.append(i), lock.release()),
                          concurrency=4)
        assert sorted(seen) == list(range(20)) and recycled == []

    def test_watchdog_fires_recycle_after_time_budget(self, monkeypatch):
        """항목은 계속 처리되고 있어도(스톨 아님) 수명이 임계를 넘으면 _recycle이 불린다."""
        pages = []
        _patch(monkeypatch, pages)
        monkeypatch.setattr(cp, 'STALL_TIMEOUT', 0.0)      # 스톨 워치독은 꺼서 재기동만 관찰
        monkeypatch.setattr(cp, 'RECYCLE_AFTER_SEC', 0.3)
        aborted = []
        monkeypatch.setattr(cp, '_abort', lambda msg: aborted.append(msg))
        fired = threading.Event()
        monkeypatch.setattr(cp, '_recycle', lambda msg: fired.set())

        release = threading.Event()

        def handle(ctx, item):
            release.wait(0.05)   # 계속 조금씩 진척은 나되(스톨 아님), 전체는 오래 걸리게

        done = threading.Event()
        # 항목을 충분히 많이 둬서(0.05s * 40 = 2s) 재기동 임계(0.3s)를 진척 중에 넘기게 한다.
        threading.Thread(target=lambda: (cp.run_crawl_pool(list(range(40)), handle, concurrency=2),
                                         done.set()), daemon=True).start()
        assert fired.wait(3), '재기동 임계(0.3s)를 넘겼는데 워치독이 재기동을 안 불렀다'
        assert aborted == []  # 스톨로 오인해 _abort를 부르면 안 됨
        release.set()
        assert done.wait(5)


class TestRecycleDrain:
    """재기동 드레인(2026-08-19) — 예전 재기동은 os._exit로 즉사시켜서 "큐에서 꺼내 처리 중이던"
    항목이 체크포인트에 못 남고 통째로 재작업이 됐다(resolve_links 실측: 완료 24.4건/사이클 대비
    폐기 14건+ = Tier1 브라우저 작업의 약 35%). 이제 재기동 시각이 되면 새 항목 공급만 끊고
    진행 중인 건은 마치게 한 뒤 종료한다 — 주기(신선도)는 그대로 두고 재작업만 없앤다."""

    def test_should_finish_drain_truth_table(self):
        assert cp._should_finish_drain(draining_for=1, inflight=3, drain_grace=90) is False  # 아직 진행 중
        assert cp._should_finish_drain(draining_for=1, inflight=0, drain_grace=90) is True   # 다 끝남 — 즉시
        assert cp._should_finish_drain(draining_for=91, inflight=3, drain_grace=90) is True  # 유예 초과 — 포기
        assert cp._should_finish_drain(draining_for=1, inflight=-1, drain_grace=90) is True  # 방어(음수)

    def test_recycle_message_distinguishes_clean_drain_from_dropped(self):
        assert '재작업 없음' in cp._recycle_message(250, 1200, 5000)              # 기존 3인자 호출 유지
        assert '3건' in cp._recycle_message(250, 1200, 5000, dropped=3)
        assert '의도된' in cp._recycle_message(250, 1200, 5000, dropped=3)        # 실패 어조 아님은 유지

    def test_inflight_items_finish_before_recycle(self, monkeypatch):
        """핵심 계약 — 재기동이 걸려도 이미 시작한 항목은 끝까지 처리된다(재작업 0건).
        시작한 항목 수와 끝낸 항목 수가 같아야 하고, 재기동은 dropped=0으로 보고돼야 한다."""
        pages = []
        _patch(monkeypatch, pages)
        monkeypatch.setattr(cp, 'STALL_TIMEOUT', 0.0)
        monkeypatch.setattr(cp, 'RECYCLE_AFTER_SEC', 0.3)
        monkeypatch.setattr(cp, 'RECYCLE_DRAIN_SEC', 5.0)
        msgs = []
        monkeypatch.setattr(cp, '_recycle', lambda msg: msgs.append(msg))
        monkeypatch.setattr(cp, '_abort', lambda msg: msgs.append('ABORT'))

        started, finished = [], []
        lock = threading.Lock()

        def handle(ctx, item):
            with lock:
                started.append(item)
            time.sleep(0.05)          # 재기동 시점에 반드시 몇 건이 '진행 중'이도록
            with lock:
                finished.append(item)

        cp.run_crawl_pool(list(range(60)), handle, concurrency=4)
        assert sorted(started) == sorted(finished), '시작만 하고 안 끝난 항목이 있다 = 재작업 발생'
        assert msgs and 'ABORT' not in msgs
        assert '재작업 없음' in msgs[-1], f'드레인이 깨끗하게 안 끝났다: {msgs[-1]}'
        assert len(finished) < 60, '재기동이 걸리기 전에 다 끝나버려 이 테스트가 의미 없어졌다'

    def test_drain_stops_taking_new_items(self, monkeypatch):
        """드레인이 시작되면 남은 큐는 손대지 않는다 — 다음 실행이 체크포인트로 이어받는 몫이다."""
        pages = []
        _patch(monkeypatch, pages)
        monkeypatch.setattr(cp, 'STALL_TIMEOUT', 0.0)
        monkeypatch.setattr(cp, 'RECYCLE_AFTER_SEC', 0.2)
        monkeypatch.setattr(cp, 'RECYCLE_DRAIN_SEC', 5.0)
        monkeypatch.setattr(cp, '_recycle', lambda msg: None)
        seen = []
        lock = threading.Lock()

        def handle(ctx, item):
            time.sleep(0.02)
            with lock:
                seen.append(item)

        cp.run_crawl_pool(list(range(200)), handle, concurrency=2)
        assert 0 < len(seen) < 200, f'드레인 후에도 큐를 계속 비웠다({len(seen)}/200)'

    def test_drain_disabled_keeps_old_immediate_exit(self, monkeypatch):
        """CRAWL_RECYCLE_DRAIN_SEC=0이면 예전처럼 즉시 종료하고, 버린 건수를 보고한다."""
        pages = []
        _patch(monkeypatch, pages)
        monkeypatch.setattr(cp, 'STALL_TIMEOUT', 0.0)
        monkeypatch.setattr(cp, 'RECYCLE_AFTER_SEC', 0.2)
        monkeypatch.setattr(cp, 'RECYCLE_DRAIN_SEC', 0.0)
        msgs = []
        fired = threading.Event()
        monkeypatch.setattr(cp, '_recycle', lambda msg: (msgs.append(msg), fired.set()))
        release = threading.Event()

        done = threading.Event()
        threading.Thread(target=lambda: (cp.run_crawl_pool(list(range(40)), lambda c, i: release.wait(0.05),
                                                           concurrency=2), done.set()), daemon=True).start()
        assert fired.wait(3), '드레인을 꺼도 재기동 자체는 발동해야 한다'
        assert '재작업 없음' not in msgs[0], '즉시 종료인데 재작업이 없다고 보고했다'
        release.set()
        assert done.wait(5)

    def test_normal_completion_never_recycles(self, monkeypatch):
        """재기동 임계에 안 걸리고 다 끝나면, join 뒤 이중확인이 오발동하면 안 된다."""
        pages = []
        _patch(monkeypatch, pages)
        monkeypatch.setattr(cp, 'STALL_TIMEOUT', 0.0)
        monkeypatch.setattr(cp, 'RECYCLE_AFTER_SEC', 30.0)   # 충분히 길어 안 걸림
        recycled = []
        monkeypatch.setattr(cp, '_recycle', lambda msg: recycled.append(msg))
        seen = []
        lock = threading.Lock()
        cp.run_crawl_pool(list(range(20)), lambda c, i: (lock.acquire(), seen.append(i), lock.release()),
                          concurrency=4)
        assert sorted(seen) == list(range(20)) and recycled == []


class TestNoPlaywrightMode:
    """브라우저 없는 빠른 패스(2026-08-18, 속도개선 공사 F단계) — use_playwright=False면
    sync_playwright() 자체를 안 띄우고(워커 수만큼 Node 드라이버 프로세스를 띄우는 비용까지
    제거) ctx.page=None으로 워커가 돈다. resolve_links의 Tier0(브라우저 없는 빠른 패스)이
    RESOLVE_FAST_CONCURRENCY(기본 200)처럼 큰 동시성을 공짜로 쓸 수 있는 근거."""

    def _boom_if_called(self, monkeypatch, name='sync_playwright'):
        def _boom(*a, **k):
            raise AssertionError(f'use_playwright=False인데 {name}가 호출됨')
        monkeypatch.setattr(cp, name, _boom)

    def test_no_sync_playwright_called_and_page_is_none(self, monkeypatch):
        self._boom_if_called(monkeypatch)
        seen_pages = []
        lock = threading.Lock()

        def handle(ctx, item):
            with lock:
                seen_pages.append(ctx.page)

        n = cp.run_crawl_pool([1, 2, 3], handle, concurrency=3, use_playwright=False)
        assert n == 3
        assert seen_pages == [None, None, None]

    def test_item_delay_still_applies_without_page(self, monkeypatch):
        self._boom_if_called(monkeypatch)
        sleeps = []
        monkeypatch.setattr(cp.time, 'sleep', sleeps.append)
        cp.run_crawl_pool([1, 2], lambda c, i: None, concurrency=1, item_delay=2.0,
                          use_playwright=False)
        assert sleeps == [2.0, 2.0]

    def test_handle_touching_page_none_is_caught_as_per_item_exception(self, monkeypatch, capsys):
        """page가 None인데 실수로 건드리면(버그) 그 항목만 예외로 건너뛰고 풀 전체는 안 죽는다
        (crawl_pool의 기존 handle 예외 계약, 2026-08-04 실측 그대로)."""
        self._boom_if_called(monkeypatch)

        def handle(ctx, item):
            ctx.page.goto('x')  # None.goto -> AttributeError

        cp.run_crawl_pool([1], handle, concurrency=1, use_playwright=False)
        assert '건너뜀' in capsys.readouterr().out

    def test_warn_hint_suppressed_without_playwright(self, monkeypatch, capsys):
        """MAX_BROWSERS 기준 경고는 브라우저를 실제로 쓸 때만 의미가 있다 — 안 쓰는 패스에서
        엉뚱하게 뜨지 않게 한다."""
        self._boom_if_called(monkeypatch)
        monkeypatch.setattr(cp, 'MAX_BROWSERS', 1)
        cp.run_crawl_pool(list(range(10)), lambda c, i: None, concurrency=10,
                          warn_hint='X_CONCURRENCY', use_playwright=False)
        assert 'X_CONCURRENCY를 낮춰보세요' not in capsys.readouterr().out

    def test_recycle_ignored_without_playwright(self, monkeypatch):
        """2026-08-18 점검 발견 버그의 재발 방지 계약 — CRAWL_RECYCLE_SEC(정기 재기동)은
        "오래 재사용된 브라우저의 메모리 누적"을 정리하려는 것이라, 브라우저 자체를 안 띄우는
        use_playwright=False 패스(resolve_links의 Tier0)에는 걸리면 안 된다. 안 그러면
        물량이 많아 이 임계 안에 못 끝내는 날마다 브라우저가 하나도 없는데 "브라우저 풀 재기동"
        명분으로 프로세스가 반복 강제종료되어, 진짜 브라우저가 필요한 Tier1 진입이 지연된다."""
        self._boom_if_called(monkeypatch)
        monkeypatch.setattr(cp, 'STALL_TIMEOUT', 0.0)       # 스톨은 안 보고 재기동만 관찰
        monkeypatch.setattr(cp, 'RECYCLE_AFTER_SEC', 0.05)  # 아주 짧게 잡아 임계를 확실히 넘김
        recycled = []
        monkeypatch.setattr(cp, '_recycle', lambda msg: recycled.append(msg))

        release = threading.Event()

        def handle(ctx, item):
            release.wait(0.02)  # 전체 실행이 재기동 임계(0.05s)를 확실히 넘도록 조금씩 걸리게

        done = threading.Event()
        threading.Thread(
            target=lambda: (cp.run_crawl_pool(list(range(20)), handle, concurrency=2,
                                              use_playwright=False),
                            done.set()),
            daemon=True).start()
        assert done.wait(5)
        assert recycled == []  # use_playwright=False면 재기동 임계를 넘겨도 절대 발동하지 않음
