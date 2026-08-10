"""update_gonggu_stage — '시작 후 N일 경과 & 종료일 없음 → 강제 종료' 규칙(2026-08-06 도입,
c9e8146 상품 이전 리팩터링에서 유실 → 2026-08-07 복원)."""
import pytest

import gonggu.update_gonggu_stage as us
from gonggu.update_gonggu_stage import stage_with_forced_end


@pytest.fixture(autouse=True)
def _freeze(monkeypatch):
    monkeypatch.setenv('GONGGU_TODAY', '2026-08-16')   # 오늘 = 8/16
    monkeypatch.setattr(us, 'FORCE_END_AFTER_DAYS', 10)


class TestForcedEnd:
    def test_start_only_10_days_elapsed_forced(self):
        # 8/6 시작 + 종료일 없음, 오늘 8/16 = 딱 10일 경과 → 강제 종료
        assert stage_with_forced_end('2026-08-06', None) == ('종료', True)

    def test_start_only_9_days_stays_inprogress(self):
        assert stage_with_forced_end('2026-08-07', None) == ('진행중', False)

    def test_explicit_end_date_never_touched(self):
        # 종료일이 명시돼 있으면 그 날짜가 진실 — 20일 지난 장기 공구도 진행중 유지
        assert stage_with_forced_end('2026-07-01', '2026-08-31') == ('진행중', False)

    def test_future_start_not_affected(self):
        assert stage_with_forced_end('2026-08-20', None) == ('시작전', False)

    def test_normal_end_transition_unchanged(self):
        # 기존 규칙 그대로: 종료일이 지났으면 (강제가 아닌) 일반 종료
        assert stage_with_forced_end('2026-08-01', '2026-08-10') == ('종료', False)

    def test_no_dates_stays_undetermined(self):
        assert stage_with_forced_end(None, None) == ('판단불가', False)

    def test_end_only_rows_unaffected(self):
        # 시작일 없이 종료일만 있는 행은 규칙 대상 아님(기존 계산 그대로)
        assert stage_with_forced_end(None, '2026-09-01') == ('진행중', False)

    def test_disabled_with_zero(self, monkeypatch):
        monkeypatch.setattr(us, 'FORCE_END_AFTER_DAYS', 0)
        assert stage_with_forced_end('2026-01-01', None) == ('진행중', False)

    def test_datetime_string_start_handled(self):
        # DB에서 str()로 넘어온 DATETIME 형태도 앞 10자리만 사용
        assert stage_with_forced_end('2026-08-01 09:00:00', None) == ('종료', True)
