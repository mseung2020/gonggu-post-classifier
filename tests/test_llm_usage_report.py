"""llm_usage_report — 모델별 단가표와 비용 계산.

2026-08-19 DeepSeek 요금 개편을 반영한다. 두 가지가 바뀌었다:
  1. 단가 자체가 올랐다 — 옛 값(input 0.14 / output 0.28 / cache_hit 0.0028)을 그대로 뒀다면
     실제 비용의 40%만 보여줬을 것이다(실측 2026-08-19 하루: 보고 $10.74 vs 실제 $27.38).
  2. **피크/오프피크**가 생겼다(오프피크 = 피크의 정확히 절반). 호출 하나하나가 UTC 몇 시에
     났느냐로 단가가 2배 갈리므로, 하루치를 모아 단가를 한 번 곱하는 방식으로는 못 센다.
"""
import datetime

import gonggu.llm_usage_report as ur


class TestPriceFor:
    def test_flash_peak_and_offpeak(self, monkeypatch):
        for k in ('PRICE_INPUT_PER_1M', 'PRICE_OUTPUT_PER_1M', 'PRICE_CACHE_HIT_PER_1M'):
            monkeypatch.delenv(k, raising=False)
        assert ur.price_for('deepseek-v4-flash', True) == {
            'input': 0.44, 'output': 1.32, 'cache_hit': 0.014}
        assert ur.price_for('deepseek-v4-flash', False) == {
            'input': 0.22, 'output': 0.66, 'cache_hit': 0.007}

    def test_pro_is_registered_now(self, monkeypatch):
        for k in ('PRICE_INPUT_PER_1M', 'PRICE_OUTPUT_PER_1M'):
            monkeypatch.delenv(k, raising=False)
        assert ur.price_for('deepseek-v4-pro', True) == {
            'input': 1.32, 'output': 3.96, 'cache_hit': 0.044}

    def test_offpeak_is_exactly_half(self, monkeypatch):
        """공식 표기가 "off-peak rates are half of the peak rates"다 — 두 벌을 손으로 적지 않고
        코드에서 나누는 이유(한쪽만 고치는 드리프트 방지)."""
        for k in ('PRICE_INPUT_PER_1M', 'PRICE_OUTPUT_PER_1M'):
            monkeypatch.delenv(k, raising=False)
        for model in ur.MODEL_PRICES_PEAK:
            peak, off = ur.price_for(model, True), ur.price_for(model, False)
            for key in peak:
                assert abs(off[key] * 2 - peak[key]) < 1e-12, (model, key)

    def test_unknown_model_none(self, monkeypatch):
        for k in ('PRICE_INPUT_PER_1M', 'PRICE_OUTPUT_PER_1M'):
            monkeypatch.delenv(k, raising=False)
        assert ur.price_for('처음보는모델', True) is None

    def test_env_overrides_all_models_and_both_slots(self, monkeypatch):
        monkeypatch.setenv('PRICE_INPUT_PER_1M', '1.0')
        monkeypatch.setenv('PRICE_OUTPUT_PER_1M', '2.0')
        monkeypatch.setenv('PRICE_CACHE_HIT_PER_1M', '0.1')
        assert ur.price_for('아무모델', True)['input'] == 1.0
        assert ur.price_for('deepseek-v4-flash', False)['output'] == 2.0  # 단가표보다 env 우선
        # env로 강제하면 피크/오프피크 구분 없이 준 값 그대로(반으로 안 나눔)
        assert ur.price_for('deepseek-v4-flash', True) == ur.price_for('deepseek-v4-flash', False)


class TestIsPeak:
    """공식: 01:00-04:00, 06:00-10:00 UTC가 피크, 나머지는 오프피크."""

    def _utc(self, hour):
        return datetime.datetime(2026, 8, 19, hour, 30, tzinfo=datetime.timezone.utc).isoformat()

    def test_peak_windows(self):
        for h in (1, 2, 3, 6, 7, 8, 9):
            assert ur.is_peak(self._utc(h)) is True, h

    def test_offpeak_windows(self):
        for h in (0, 4, 5, 10, 11, 15, 23):
            assert ur.is_peak(self._utc(h)) is False, h

    def test_boundaries_are_half_open(self):
        """04:00과 10:00은 피크가 끝나는 시각 — 오프피크다."""
        assert ur.is_peak(self._utc(4)) is False
        assert ur.is_peak(self._utc(10)) is False
        assert ur.is_peak(self._utc(3)) is True
        assert ur.is_peak(self._utc(9)) is True

    def test_timezone_aware_string_converted_to_utc(self):
        """KST 19:00 = UTC 10:00 → 오프피크. 오프셋을 무시하고 로컬 시각으로 읽으면 틀린다."""
        assert ur.is_peak('2026-08-19T19:00:00+09:00') is False
        assert ur.is_peak('2026-08-19T18:00:00+09:00') is True    # = UTC 09:00

    def test_naive_legacy_timestamp_uses_local_tz(self):
        """2026-08-19 이전 기록은 타임존 없는 로컬 시각이다 — 로컬로 해석해야 맞다."""
        naive = '2026-08-19T12:00:00'
        local_utc_hour = (datetime.datetime.fromisoformat(naive).astimezone()
                          .astimezone(datetime.timezone.utc).hour)
        expected = any(lo <= local_utc_hour < hi for lo, hi in ur.PEAK_HOURS_UTC)
        assert ur.is_peak(naive) is expected

    def test_unparseable_timestamp_is_offpeak(self):
        """시각을 못 읽으면 싼 쪽으로 — 비용을 부풀려 보고하지 않는다."""
        assert ur.is_peak('') is False
        assert ur.is_peak(None) is False
        assert ur.is_peak('어제') is False


class TestCostUsd:
    def test_cache_split_billing_offpeak(self):
        # 입력 1M 중 캐시히트 60만: miss 40만×$0.22 + hit 60만×$0.007 + 출력 10만×$0.66
        totals = {'prompt': 1_000_000, 'cache_hit': 600_000, 'cache_miss': 400_000,
                  'completion': 100_000}
        prices = ur.price_for('deepseek-v4-flash', False)
        expected = (400_000 * 0.22 + 600_000 * 0.007 + 100_000 * 0.66) / 1_000_000
        assert abs(ur.cost_usd(totals, prices) - expected) < 1e-9

    def test_peak_is_double_offpeak(self):
        totals = {'prompt': 1_000_000, 'cache_hit': 600_000, 'cache_miss': 400_000,
                  'completion': 100_000}
        off = ur.cost_usd(totals, ur.price_for('deepseek-v4-flash', False))
        peak = ur.cost_usd(totals, ur.price_for('deepseek-v4-flash', True))
        assert abs(peak - off * 2) < 1e-9

    def test_prefers_logged_cache_miss(self):
        """로그에 cache_miss_tokens가 있으면 그 값을 쓴다 — prompt-hit 유도보다 정확하다
        (reasoning 토큰 등으로 prompt = hit + miss가 안 맞는 경우가 있을 수 있다)."""
        totals = {'prompt': 999_999, 'cache_hit': 600_000, 'cache_miss': 400_000, 'completion': 0}
        prices = ur.price_for('deepseek-v4-flash', False)
        assert abs(ur.cost_usd(totals, prices) - (400_000 * 0.22 + 600_000 * 0.007) / 1e6) < 1e-9

    def test_falls_back_when_cache_miss_absent(self):
        """cache_miss가 없는 옛 기록은 prompt - cache_hit으로 유도한다."""
        totals = {'prompt': 1_000_000, 'cache_hit': 600_000, 'completion': 0}
        prices = ur.price_for('deepseek-v4-flash', False)
        assert abs(ur.cost_usd(totals, prices) - (400_000 * 0.22 + 600_000 * 0.007) / 1e6) < 1e-9

    def test_no_cache_hits(self):
        totals = {'prompt': 1_000_000, 'cache_hit': 0, 'cache_miss': 1_000_000, 'completion': 0}
        assert abs(ur.cost_usd(totals, ur.price_for('deepseek-v4-flash', False)) - 0.22) < 1e-9
