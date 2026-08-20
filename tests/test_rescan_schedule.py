"""rescan_inprogress의 스케줄 규칙(2026-08-06 재공사) — 요구사항을 테스트로 박제:
① 신규 전환(이력 없음)은 무조건 포함, ② error는 무조건 포함, ③ 그 외는 백오프 후 은퇴."""
import datetime

import pytest

import gonggu.rescan_inprogress as rs
from gonggu.rescan_inprogress import classify_target, next_due

TODAY = datetime.date(2026, 8, 6)
TODAY_ISO = '2026-08-06'


class TestNextDue:
    def test_backoff_progression(self, monkeypatch):
        monkeypatch.setattr(rs, 'BACKOFF_DAYS', [1, 2, 4, 7])
        assert next_due(1, TODAY) == '2026-08-07'   # 1차 시도 후 +1일
        assert next_due(2, TODAY) == '2026-08-08'   # 2차 시도 후 +2일
        assert next_due(3, TODAY) == '2026-08-10'   # 3차 시도 후 +4일
        assert next_due(4, TODAY) == '2026-08-13'   # 4차 시도 후 +7일

    def test_exhausted_returns_none(self, monkeypatch):
        monkeypatch.setattr(rs, 'BACKOFF_DAYS', [1, 2, 4, 7])
        assert next_due(5, TODAY) is None            # 5차(마지막) 시도 후 은퇴


class TestClassifyTarget:
    def test_requirement_1_fresh_transition_always_due(self):
        """① 진행중으로 새로 넘어온(이력 없는) 상품은 무조건 당일 대상."""
        due, reason = classify_target('unresolved', None, TODAY_ISO)
        assert due and reason == '신규전환'

    def test_requirement_2_error_due_regardless_of_backoff_schedule(self):
        """② error는 unresolved/hold의 백오프 날짜(next_due)와 무관하게 대상 —
        (그 상품이 unresolved였다면 이미 은퇴했을 next_due=None이어도 error는 여전히 포함)."""
        would_be_retired_if_unresolved = {'attempts': 3, 'next_due': None}
        due, reason = classify_target('error', would_be_retired_if_unresolved, TODAY_ISO)
        assert due and '에러' in reason

    def test_error_retires_after_max_attempts(self, monkeypatch):
        """2026-08-18 추가 — error도 무한정은 아니다. RESCAN_ERROR_MAX_ATTEMPTS를 넘게
        계속 error로만 끝난 상품은 은퇴시켜, 영구적 기술 문제가 매일 무한 재시도되는 것을
        막는다(unresolved/hold의 백오프 소진 은퇴와 대칭)."""
        monkeypatch.setattr(rs, 'RESCAN_ERROR_MAX_ATTEMPTS', 14)
        just_under = {'attempts': 13, 'next_due': None}
        at_cap = {'attempts': 14, 'next_due': None}
        over_cap = {'attempts': 20, 'next_due': None}
        assert classify_target('error', just_under, TODAY_ISO)[0] is True
        due, reason = classify_target('error', at_cap, TODAY_ISO)
        assert due is False and '은퇴' in reason
        assert classify_target('error', over_cap, TODAY_ISO)[0] is False

    def test_error_without_history_still_always_due(self):
        """이력이 아예 없는 error(예: 방금 처음 error로 떨어진 상품)는 당연히 포함."""
        due, reason = classify_target('error', None, TODAY_ISO)
        assert due and '에러' in reason

    def test_cooldown_not_due(self):
        rec = {'attempts': 1, 'next_due': '2026-08-07'}  # 내일 예정
        due, reason = classify_target('unresolved', rec, TODAY_ISO)
        assert not due and '쿨다운' in reason

    def test_backoff_arrival_due(self):
        assert classify_target('unresolved', {'attempts': 1, 'next_due': '2026-08-06'}, TODAY_ISO)[0]
        assert classify_target('hold', {'attempts': 2, 'next_due': '2026-08-01'}, TODAY_ISO)[0]

    def test_retired_never_due(self):
        due, reason = classify_target('unresolved', {'attempts': 5, 'next_due': None}, TODAY_ISO)
        assert not due and '은퇴' in reason

    def test_force_overrides_schedule(self):
        assert classify_target('unresolved', {'attempts': 5, 'next_due': None}, TODAY_ISO, force=True)[0]
        assert classify_target('hold', {'attempts': 1, 'next_due': '2026-09-01'}, TODAY_ISO, force=True)[0]


class TestLifecycle:
    def test_full_life_of_an_unresolved_product(self, monkeypatch):
        """전환일 포함 최대 5회 시도 후 은퇴하는 전체 수명 시나리오."""
        monkeypatch.setattr(rs, 'BACKOFF_DAYS', [1, 2, 4, 7])
        rec = None
        day = datetime.date(2026, 8, 6)
        attempt_days = []
        for _ in range(30):  # 한 달을 하루씩 진행
            due, _ = classify_target('unresolved', rec, day.isoformat())
            if due:
                attempts = (rec.get('attempts', 0) if rec else 0) + 1
                attempt_days.append(day.isoformat())
                rec = {'attempts': attempts, 'next_due': next_due(attempts, day)}
            day += datetime.timedelta(days=1)
        assert attempt_days == ['2026-08-06', '2026-08-07', '2026-08-09',
                                '2026-08-13', '2026-08-20']  # 5회, 이후 영구 보류
        assert rec['next_due'] is None


class TestUnknownStageArm:
    """stage='판단불가' 상품을 재탐색 대상에 넣는 선택지(2026-08-19).

    교착 때문에 생겼다: 링크를 찾으려면 rescan이 필요한데 rescan은 stage='진행중'만 보고,
    stage가 진행중이 되려면 기간이 필요한데, 기간을 찾는 backfill_period의 몰 크롤은
    link_status='done'만 본다 — 링크가 unresolved면 어느 쪽도 못 들어간다.
    실측(2026-08-19): unresolved+hold 27518건 중 rescan이 보던 건 1643건(6%)뿐이었고
    판단불가에 갇힌 게 9827건이었다. 이 팔을 켜면 후보 풀이 3058 → 11803으로 늘었다."""

    def test_off_by_default_keeps_old_sql(self):
        from gonggu.platforms import PLATFORMS
        sql = rs._select_sql(PLATFORMS['ig'], 0)
        assert '판단불가' not in sql
        assert "gonggu_stage = '진행중'" in sql and "link_status = 'error'" in sql

    def test_on_adds_recent_unknown_stage_arm(self):
        from gonggu.platforms import PLATFORMS
        p = PLATFORMS['ig']
        sql = rs._select_sql(p, 30)
        assert "gonggu_stage = '판단불가'" in sql
        assert 'DATE_SUB(CURDATE(), INTERVAL %s DAY)' in sql   # 기간은 파라미터로
        assert f'p.{p.date_col}' in sql
        # 기존 두 팔은 그대로 살아있어야 한다(회귀 방지)
        assert "gonggu_stage = '진행중'" in sql and "link_status = 'error'" in sql

    def test_placeholder_count_matches_bound_params(self):
        """SQL의 %s 개수와 _fetch_candidates가 넘기는 파라미터 개수가 어긋나면 실행 시점에야
        터진다 — 여기서 못박는다."""
        from gonggu.platforms import PLATFORMS
        for p in PLATFORMS.values():
            assert rs._select_sql(p, 0).count('%s') == 0
            assert rs._select_sql(p, 30).count('%s') == 1

    def test_unknown_stage_products_follow_the_same_schedule(self):
        """판단불가라고 특별 취급하지 않는다 — 이력 없으면 신규전환, 그 뒤엔 백오프/은퇴."""
        assert classify_target('unresolved', None, TODAY_ISO)[0] is True
        exhausted = {'attempts': 5, 'next_due': None}
        assert classify_target('unresolved', exhausted, TODAY_ISO)[0] is False
