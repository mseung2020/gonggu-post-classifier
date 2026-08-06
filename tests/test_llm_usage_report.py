"""llm_usage_report — 모델별 단가표(2026-08-06 확인된 DeepSeek Flash 공식 단가) 비용 계산."""
import gonggu.llm_usage_report as ur


class TestPriceFor:
    def test_flash_official_prices(self, monkeypatch):
        for k in ('PRICE_INPUT_PER_1M', 'PRICE_OUTPUT_PER_1M', 'PRICE_CACHE_HIT_PER_1M'):
            monkeypatch.delenv(k, raising=False)
        p = ur.price_for('deepseek-v4-flash')
        assert p == {'input': 0.14, 'output': 0.28, 'cache_hit': 0.0028}

    def test_unknown_model_none(self, monkeypatch):
        for k in ('PRICE_INPUT_PER_1M', 'PRICE_OUTPUT_PER_1M'):
            monkeypatch.delenv(k, raising=False)
        assert ur.price_for('deepseek-v4-pro') is None  # 프로 단가는 아직 미확인

    def test_env_overrides_all_models(self, monkeypatch):
        monkeypatch.setenv('PRICE_INPUT_PER_1M', '1.0')
        monkeypatch.setenv('PRICE_OUTPUT_PER_1M', '2.0')
        monkeypatch.setenv('PRICE_CACHE_HIT_PER_1M', '0.1')
        assert ur.price_for('아무모델')['input'] == 1.0
        assert ur.price_for('deepseek-v4-flash')['output'] == 2.0  # 단가표보다 env 우선


class TestCostUsd:
    def test_cache_split_billing(self):
        # 입력 1M 중 캐시히트 60만: miss 40만×$0.14 + hit 60만×$0.0028 + 출력 10만×$0.28
        totals = {'prompt': 1_000_000, 'cache_hit': 600_000, 'completion': 100_000}
        prices = ur.MODEL_PRICES['deepseek-v4-flash']
        expected = (400_000 * 0.14 + 600_000 * 0.0028 + 100_000 * 0.28) / 1_000_000
        assert abs(ur.cost_usd(totals, prices) - expected) < 1e-9
        assert abs(ur.cost_usd(totals, prices) - 0.08568) < 1e-9

    def test_no_cache_hits(self):
        totals = {'prompt': 1_000_000, 'cache_hit': 0, 'completion': 0}
        assert abs(ur.cost_usd(totals, ur.MODEL_PRICES['deepseek-v4-flash']) - 0.14) < 1e-9
