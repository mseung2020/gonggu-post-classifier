"""공구기간 백필의 순수 로직 — 2026-08-18에 옛 backfill_period_inpock(인포크 텍스트, 크롤
없음)과 backfill_period(몰 크롤)를 하나의 2단 에스컬레이션으로 병합했다(문제 10). 여기서는
그 병합의 핵심(_inpock_text 텍스트 수집, _should_skip 재시도 정책, _source_sig 대상
선별)만 검증한다 — 실제 LLM/DB/크롤은 실전 스모크로 확인(이 저장소 규약).

2026-08-19: 재시도 정책이 "쿨다운 지나면 또" 에서 "읽을 소스가 달라졌을 때만"으로 바뀌었다
(_source_sig). 근거는 실측 — 2회차 이상에서 기간을 찾은 35건이 전부 몰 크롤에서 나왔고(1회차엔
없던 소스가 생긴 것), 소스가 그대로인 재시도 6468회가 건진 건 35건(0.54%)뿐이었다."""
import datetime

import gonggu.backfill_period as bp
from gonggu.backfill_period import _inpock_text, _should_skip, _source_sig

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
        assert _should_skip(None, TODAY, set()) is False

    def test_found_is_permanently_skipped(self):
        assert _should_skip({'status': 'found', 'attempts': 1, 'checked_at': '2026-08-01'}, TODAY, set()) is True

    def test_within_cooldown_skipped(self, monkeypatch):
        monkeypatch.setattr(bp, 'RETRY_COOLDOWN_DAYS', 5)
        rec = {'status': 'not_found', 'attempts': 1, 'checked_at': '2026-08-15'}  # 3일 전
        assert _should_skip(rec, TODAY, set()) is True

    def test_cooldown_expired_not_skipped(self, monkeypatch):
        monkeypatch.setattr(bp, 'RETRY_COOLDOWN_DAYS', 5)
        rec = {'status': 'not_found', 'attempts': 1, 'checked_at': '2026-08-10'}  # 8일 전
        assert _should_skip(rec, TODAY, set()) is False

    def test_max_attempts_retires_regardless_of_cooldown(self, monkeypatch):
        monkeypatch.setattr(bp, 'MAX_ATTEMPTS', 3)
        rec = {'status': 'not_found', 'attempts': 3, 'checked_at': '2026-01-01'}  # 쿨다운은 진작 끝났지만
        assert _should_skip(rec, TODAY, set()) is True


class TestSourceSignature:
    """인포크 텍스트도 없고 링크도 미확정이면 이번 실행에서 검사할 방법이 없다 — 지문이 빈
    집합이 되고, 호출부가 그걸 보고 재시도 횟수를 늘리지 않고 조용히 건너뛴다."""

    def test_inpock_text_gives_signature(self):
        assert _source_sig({('ig', 'p1'): '텍스트'}, 'ig', {'post_id': 'p1', 'link_status': None})

    def test_confirmed_link_gives_mall_signature(self):
        sig = _source_sig({}, 'ig', {'post_id': 'p2', 'link_status': 'done',
                                     'candidate_url': 'https://mall/x'})
        assert len(sig) == 1 and next(iter(sig)).startswith('mall:')

    def test_no_source_at_all(self):
        assert _source_sig({}, 'ig', {'post_id': 'p3', 'link_status': 'unresolved'}) == set()

    def test_signature_changes_with_content(self):
        """인포크 텍스트가 바뀌면 지문도 바뀐다 — 크리에이터가 나중에 기간을 적어 넣는 경우를
        놓치지 않기 위해 길이가 아니라 내용 해시를 쓴다."""
        r = {'post_id': 'p', 'link_status': None}
        a = _source_sig({('ig', 'p'): '기간 미정'}, 'ig', r)
        b = _source_sig({('ig', 'p'): '8/1~8/5 공구'}, 'ig', r)
        assert a and b and a != b

    def test_both_sources(self):
        sig = _source_sig({('ig', 'p'): 't'}, 'ig',
                          {'post_id': 'p', 'link_status': 'done', 'candidate_url': 'u'})
        assert len(sig) == 2


class TestRetryOnNewSourceOnly:
    """핵심 정책 전환(2026-08-19) — 같은 소스를 같은 내용으로 다시 LLM에 태우면 답도 같다.
    재시도는 '읽을 거리가 달라졌을 때'만 값을 한다."""

    def test_same_source_is_not_retried(self):
        """예전엔 쿨다운만 지나면 또 읽었다 — 그 재시도 6468회가 건진 게 35건(0.54%)이었다."""
        rec = {'status': 'not_found', 'attempts': 1, 'checked_at': '2026-01-01',
               'sources': ['inpock:aaaa1111']}
        assert _should_skip(rec, TODAY, {'inpock:aaaa1111'}) is True

    def test_new_mall_source_triggers_retry_even_within_cooldown(self, monkeypatch):
        """실측상 2회차 성공 35건이 전부 이 경우였다 — 1회차엔 링크가 unresolved라 몰 크롤이
        불가능했고, 그 사이 link_status가 done이 되면서 새 소스가 생겼다."""
        monkeypatch.setattr(bp, 'RETRY_COOLDOWN_DAYS', 5)
        rec = {'status': 'not_found', 'attempts': 1, 'checked_at': '2026-08-17',  # 어제 = 쿨다운 중
               'sources': ['inpock:aaaa1111']}
        assert _should_skip(rec, TODAY, {'inpock:aaaa1111', 'mall:bbbb2222'}) is False

    def test_changed_inpock_text_triggers_retry(self):
        rec = {'status': 'not_found', 'attempts': 1, 'checked_at': '2026-08-17',
               'sources': ['inpock:aaaa1111']}
        assert _should_skip(rec, TODAY, {'inpock:cccc3333'}) is False

    def test_max_attempts_still_caps_runaway(self, monkeypatch):
        """새 소스가 계속 생겨도 폭주 상한은 남긴다."""
        monkeypatch.setattr(bp, 'MAX_ATTEMPTS', 3)
        rec = {'status': 'not_found', 'attempts': 3, 'sources': ['inpock:a']}
        assert _should_skip(rec, TODAY, {'mall:zzzz'}) is True

    def test_found_wins_over_new_source(self):
        rec = {'status': 'found', 'attempts': 1, 'sources': ['inpock:a']}
        assert _should_skip(rec, TODAY, {'inpock:a', 'mall:b'}) is True

    def test_legacy_record_keeps_old_rule(self, monkeypatch):
        """sources가 없는 옛 기록은 지문을 모르니 예전 규칙(쿨다운+횟수)을 그대로 쓴다 —
        갑자기 7481건이 전부 재시도 대상이 되는 걸 막는다. 실측상 손해도 없었다(옛 규칙으로
        영구 스킵된 1052건 중 지금 몰 소스가 생긴 건 0건)."""
        monkeypatch.setattr(bp, 'RETRY_COOLDOWN_DAYS', 5)
        rec = {'status': 'not_found', 'attempts': 1, 'checked_at': '2026-08-17'}  # 어제
        assert _should_skip(rec, TODAY, {'mall:new'}) is True     # 새 소스가 있어도 쿨다운 우선
        rec2 = {'status': 'not_found', 'attempts': 1, 'checked_at': '2026-08-01'}  # 쿨다운 지남
        assert _should_skip(rec2, TODAY, set()) is False
