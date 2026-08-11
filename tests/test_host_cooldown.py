"""호스트별 적응형 쿨다운(2026-08-11) — 429/403을 준 호스트만 잠깐 쉬어 레이트리밋 폭발을 막는다."""
import time

from gonggu.resolve_links import browser


def test_no_wait_when_host_free(monkeypatch):
    monkeypatch.setattr(browser, '_HOST_COOLDOWN_SEC', 20.0)
    browser._host_cooldown.clear()
    t0 = time.time()
    browser._cooldown_wait('never-blocked.example.com')   # 쿨다운 없음 → 즉시
    assert time.time() - t0 < 0.5


def test_mark_blocked_registers_future(monkeypatch):
    monkeypatch.setattr(browser, '_HOST_COOLDOWN_SEC', 20.0)
    browser._host_cooldown.clear()
    browser._mark_blocked('smartstore.naver.com')
    assert browser._host_cooldown['smartstore.naver.com'] > time.time()


def test_cooldown_waits_briefly(monkeypatch):
    monkeypatch.setattr(browser, '_HOST_COOLDOWN_SEC', 0.3)
    browser._host_cooldown.clear()
    browser._mark_blocked('y.example.com')
    t0 = time.time()
    browser._cooldown_wait('y.example.com')               # 쿨다운 중 → 대략 그만큼 대기
    assert time.time() - t0 >= 0.2


def test_disabled_when_zero(monkeypatch):
    monkeypatch.setattr(browser, '_HOST_COOLDOWN_SEC', 0.0)
    browser._host_cooldown.clear()
    browser._mark_blocked('z.example.com')
    assert 'z.example.com' not in browser._host_cooldown  # 0이면 끔(등록 안 함)
    t0 = time.time()
    browser._cooldown_wait('z.example.com')
    assert time.time() - t0 < 0.5
