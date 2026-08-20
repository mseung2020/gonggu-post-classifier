"""uc 신뢰 게이트(2026-08-20) 계약 — 크롬/셀레늄 없이 판정 로직만 검증한다.

게이트가 지켜야 할 것 세 가지:
  1) 쿠키가 살아있으면 프로필을 지우지도, 워밍업을 띄우지도 않는다(예전엔 매일 rm -rf 했다).
  2) 죽었는데 stdin이 TTY가 아니면(cron/nohup) 워밍업을 시도조차 안 하고 uc 단계를 건너뛴다 —
     TTY가 아니면 입력이 영영 안 오므로 기다리는 건 시간을 버리는 것이다.
  3) 판정은 실행 1회당 한 번(probe도 크롬 창을 띄운다).
"""
import gonggu.uc_gate as uc_gate


class TestDecide:
    def test_trust_alive_skips_warmup(self):
        action, reason = uc_gate.decide(trust_ok=True, interactive=True)
        assert action == 'ok'
        assert '생략' in reason

    def test_trust_alive_even_without_tty(self):
        # 무인 실행이어도 쿠키만 살아있으면 uc 단계를 돌 수 있다 — 여기가 통합의 핵심.
        assert uc_gate.decide(trust_ok=True, interactive=False)[0] == 'ok'

    def test_expired_with_tty_asks_for_warmup(self):
        assert uc_gate.decide(trust_ok=False, interactive=True)[0] == 'warmup'

    def test_expired_without_tty_skips_uc(self):
        action, reason = uc_gate.decide(trust_ok=False, interactive=False)
        assert action == 'skip'
        assert 'TTY' in reason


class TestSafeProfilePath:
    def test_accepts_real_profile_paths(self):
        assert uc_gate.is_safe_profile_path('/Users/rachel/.gonggu_uc_profile')
        assert uc_gate.is_safe_profile_path('/Users/rachel/.gonggu_uc_profile_1')
        assert uc_gate.is_safe_profile_path('/repo/data/auth/uc_profile')

    def test_rejects_dangerous_paths(self):
        # UC_PROFILE에 오타/빈 값이 들어와도 엉뚱한 곳을 rm -rf 하면 안 된다.
        for bad in ('', None, '/', '~', '/Users', '/Users/rachel', '/tmp', '/repo/data'):
            assert not uc_gate.is_safe_profile_path(bad), bad


class TestEnsureTrust:
    def setup_method(self):
        uc_gate.reset_cache()

    def test_alive_does_not_touch_profile_or_warmup(self):
        touched = []
        ok, _ = uc_gate.ensure_trust(
            printer=lambda *a: None, interactive=True,
            prober=lambda: (True, '신뢰 정상'),
            resetter=lambda p, printer=None: touched.append('reset'),
            warmer=lambda printer=None: touched.append('warm') or (True, ''))
        assert ok is True
        assert touched == []

    def test_expired_without_tty_does_not_warm_up(self):
        touched = []
        ok, reason = uc_gate.ensure_trust(
            printer=lambda *a: None, interactive=False,
            prober=lambda: (False, '신뢰 만료(로그인월/캡차 감지)'),
            resetter=lambda p, printer=None: touched.append('reset'),
            warmer=lambda printer=None: touched.append('warm') or (True, ''))
        assert ok is False
        assert touched == []
        assert 'TTY' in reason

    def test_expired_with_tty_resets_then_warms_up(self):
        touched = []
        ok, _ = uc_gate.ensure_trust(
            printer=lambda *a: None, interactive=True,
            prober=lambda: (False, '신뢰 만료(로그인월/캡차 감지)'),
            resetter=lambda p, printer=None: touched.append('reset') or True,
            warmer=lambda printer=None: (touched.append('warm'), (True, '워밍업 완료'))[1])
        assert ok is True
        assert touched == ['reset', 'warm']   # 순서도 중요 — 지운 뒤에 쌓는다

    def test_warmup_failure_skips_uc(self):
        ok, reason = uc_gate.ensure_trust(
            printer=lambda *a: None, interactive=True,
            prober=lambda: (False, '신뢰 만료'),
            resetter=lambda p, printer=None: True,
            warmer=lambda printer=None: (False, '워밍업 600초 내 무응답'))
        assert ok is False
        assert '무응답' in reason

    def test_probe_runs_only_once_per_run(self):
        calls = []

        def prober():
            calls.append(1)
            return True, '신뢰 정상'

        uc_gate.ensure_trust(printer=lambda *a: None, interactive=True, prober=prober)
        uc_gate.ensure_trust(printer=lambda *a: None, interactive=True, prober=prober)
        assert len(calls) == 1     # 두 번째는 캐시 — 크롬 창이 또 뜨면 안 된다

    def test_force_rechecks(self):
        calls = []

        def prober():
            calls.append(1)
            return True, '신뢰 정상'

        uc_gate.ensure_trust(printer=lambda *a: None, interactive=True, prober=prober)
        uc_gate.ensure_trust(printer=lambda *a: None, interactive=True, prober=prober, force=True)
        assert len(calls) == 2


class TestProbe:
    """uc_healthcheck.probe는 어떤 경우에도 예외를 안 내고 (ok, 사유)를 돌려줘야 한다 —
    이 판정 하나 때문에 데일리가 멈추면 안 된다. 가짜 uc_engine을 sys.modules에 꽂아 검증한다."""

    def _with_fake_engine(self, monkeypatch, fetch):
        import sys
        import types
        closed = []
        fake = types.ModuleType('gonggu.uc_engine')
        fake.fetch_sync = fetch
        fake.close_sync = lambda: closed.append(1)
        fake.looks_challenged = lambda html: '캡차' in html
        fake.DEFAULT_PROFILE = '/tmp/.gonggu_uc_profile'
        monkeypatch.setitem(sys.modules, 'gonggu.uc_engine', fake)
        return closed

    def test_healthy_page_is_ok(self, monkeypatch):
        from gonggu.uc_healthcheck import probe
        closed = self._with_fake_engine(
            monkeypatch, lambda url: ('https://smartstore.naver.com/main', '<html>상품</html>'))
        ok, reason = probe()
        assert ok is True and reason == '신뢰 정상'
        assert closed == [1]        # 창은 반드시 닫는다

    def test_login_redirect_is_expired(self, monkeypatch):
        from gonggu.uc_healthcheck import probe
        self._with_fake_engine(monkeypatch, lambda url: ('https://nid.naver.com/nidlogin.login', '<html/>'))
        ok, reason = probe()
        assert ok is False and '신뢰 만료' in reason

    def test_captcha_is_expired(self, monkeypatch):
        from gonggu.uc_healthcheck import probe
        self._with_fake_engine(monkeypatch, lambda url: ('https://smartstore.naver.com/main', '캡차'))
        assert probe()[0] is False

    def test_engine_exception_does_not_raise(self, monkeypatch):
        from gonggu.uc_healthcheck import probe

        def boom(url):
            raise RuntimeError('Chrome이 예기치 않게 종료')

        closed = self._with_fake_engine(monkeypatch, boom)
        ok, reason = probe()
        assert ok is False and '실행 실패' in reason
        assert closed == [1]        # 실패 경로에서도 드라이버를 닫는다


class TestDefaultProber:
    """프로필이 없으면 크롬을 안 띄운다(2026-08-20) — 첫 실행/rm -rf 직후에 항상 걸리는 경로."""

    def test_missing_profile_short_circuits(self, monkeypatch, tmp_path):
        opened = []
        monkeypatch.setattr(uc_gate, 'profile_path', lambda: str(tmp_path / 'nope'))
        monkeypatch.setattr('gonggu.uc_healthcheck.probe',
                            lambda: opened.append(1) or (True, '신뢰 정상'))
        ok, reason = uc_gate._default_prober()
        assert ok is False and '프로필 없음' in reason
        assert opened == []          # 크롬을 띄우지 않았다

    def test_existing_profile_delegates_to_probe(self, monkeypatch, tmp_path):
        profile = tmp_path / '.gonggu_uc_profile'
        profile.mkdir()
        monkeypatch.setattr(uc_gate, 'profile_path', lambda: str(profile))
        monkeypatch.setattr('gonggu.uc_healthcheck.probe', lambda: (True, '신뢰 정상'))
        assert uc_gate._default_prober() == (True, '신뢰 정상')

    def test_profile_exists_helper(self, tmp_path):
        assert uc_gate.profile_exists(str(tmp_path)) is True
        assert uc_gate.profile_exists(str(tmp_path / 'nope')) is False
        assert uc_gate.profile_exists('') is False
        assert uc_gate.profile_exists(None) is False
