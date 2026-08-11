"""인포크 우선 기간 백필의 순수 로직 — 인포크 파싱본에서 기간이 있을 만한 텍스트를 제대로
모으는지(_inpock_text). 실제 LLM/DB는 실전 스모크로 확인(이 저장소 규약)."""
from gonggu.backfill_period_inpock import _inpock_text


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
