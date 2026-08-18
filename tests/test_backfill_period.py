"""공구기간 백필의 순수 로직 — 2026-08-18에 옛 backfill_period_inpock(인포크 텍스트, 크롤
없음)과 backfill_period(몰 크롤)를 하나의 2단 에스컬레이션으로 병합했다(문제 10). 여기서는
그 병합의 핵심(_inpock_text 텍스트 수집, _should_skip 재시도 정책, _has_any_source 대상
선별)만 검증한다 — 실제 LLM/DB/크롤은 실전 스모크로 확인(이 저장소 규약)."""
import datetime

import gonggu.backfill_period as bp
from gonggu.backfill_period import _has_any_source, _inpock_text, _should_skip

TODAY = datetime.date(2026, 8, 18)


def test_collects_link_titles_and_texts():
    d = {
        'title': '또우맘 공구', 'bio': '육아템 공구', 'notice': '8월 일정',
        'texts': ['🎁 이벤트 안내'],
        'links': [
            {'title': 'OPEN 8.7 10시 ~ 8.10 23:59 [베른호이체 미니피아노]'},
            {'title': 'D-14 8.24~8.27 원형행거'},
        ],
        'smart_stores': [{'title': '라무르', 'products': [{'name': '선크림'}]}],
        'collections': [{'title': '주방', 'products': [{'name': '밀폐용기'}]}],
    }
    text = _inpock_text(d)
    # 기간 문구가 들어있는 링크 제목이 포함돼야(LLM이 상품명 매칭해 기간을 뽑음)
    assert 'OPEN 8.7 10시 ~ 8.10 23:59' in text
    assert '베른호이체 미니피아노' in text
    assert '8.24~8.27' in text
    # 소개/공지/텍스트블록/스토어·컬렉션 상품명도 포함
    assert '8월 일정' in text and '이벤트 안내' in text
    assert '선크림' in text and '밀폐용기' in text


def test_empty_and_bad_input_safe():
    assert _inpock_text(None) == ''
    assert _inpock_text({}) == ''
    assert _inpock_text({'links': [{}], 'texts': []}) == ''   # 제목 없는 링크는 무시


class TestShouldSkip:
    """병합 후 하나로 통일된 재시도 정책(문제 7/10 수정) — 어느 티어(인포크/몰 크롤)에서 나온
    기록이든 정책은 하나뿐이다."""

    def test_no_history_not_skipped(self):
        assert _should_skip(None, TODAY) is False

    def test_found_is_permanently_skipped(self):
        assert _should_skip({'status': 'found', 'attempts': 1, 'checked_at': '2026-08-01'}, TODAY) is True

    def test_within_cooldown_skipped(self, monkeypatch):
        monkeypatch.setattr(bp, 'RETRY_COOLDOWN_DAYS', 5)
        rec = {'status': 'not_found', 'attempts': 1, 'checked_at': '2026-08-15'}  # 3일 전
        assert _should_skip(rec, TODAY) is True

    def test_cooldown_expired_not_skipped(self, monkeypatch):
        monkeypatch.setattr(bp, 'RETRY_COOLDOWN_DAYS', 5)
        rec = {'status': 'not_found', 'attempts': 1, 'checked_at': '2026-08-10'}  # 8일 전
        assert _should_skip(rec, TODAY) is False

    def test_max_attempts_retires_regardless_of_cooldown(self, monkeypatch):
        monkeypatch.setattr(bp, 'MAX_ATTEMPTS', 3)
        rec = {'status': 'not_found', 'attempts': 3, 'checked_at': '2026-01-01'}  # 쿨다운은 진작 끝났지만
        assert _should_skip(rec, TODAY) is True


class TestHasAnySource:
    """인포크 텍스트도 없고 링크도 미확정이면 이번 실행에서 검사할 방법이 없다 —
    재시도 횟수를 늘리지 않고 조용히 건너뛴다(병합 전 backfill_period_inpock의
    no_inpock 스킵과 backfill_period의 link_status=done 요구를 하나로 통합)."""

    def test_has_inpock_text(self):
        inpock = {('ig', 'p1'): '텍스트'}
        r = {'post_id': 'p1', 'link_status': None}
        assert _has_any_source(inpock, 'ig', r) is True

    def test_has_confirmed_link_without_inpock(self):
        r = {'post_id': 'p2', 'link_status': 'done'}
        assert _has_any_source({}, 'ig', r) is True

    def test_no_source_at_all(self):
        r = {'post_id': 'p3', 'link_status': 'unresolved'}
        assert _has_any_source({}, 'ig', r) is False
