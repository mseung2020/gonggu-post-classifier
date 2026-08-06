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

    def test_requirement_2_error_always_due(self):
        """② error는 이력/스케줄과 무관하게 무조건 대상."""
        retired = {'attempts': 99, 'next_due': None}
        due, reason = classify_target('error', retired, TODAY_ISO)
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
