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

    def release_if_contended(self):
        self.released += 1

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
