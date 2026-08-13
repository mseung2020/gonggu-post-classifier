"""crawl_pool.py — 공용 워커 풀(2단계 B3)의 계약: 전 항목 정확히 1회 처리, 워커당 자원
setup/teardown, 예외에도 워커 생존, 세션 저장은 워커 0번만. 실제 Playwright 없이
LazyPage/sync_playwright를 페이크로 바꿔 검증한다."""
import threading

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
