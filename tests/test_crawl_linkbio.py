"""crawl_linkbio의 순수 로직 — 캡션/프로필에서 인포크 허브 URL을 정확히 뽑는지 못박는다.
(실제 인포크 크롤/DB는 LIMIT 소량 실전 스모크로 확인하는 게 이 저장소 규약.)"""
from gonggu.crawl_linkbio import _date, extract_inpock_hubs


def test_hub_from_caption():
    cap = '공구 링크는 프로필 https://link.inpock.co.kr/2ddou_ 확인하세요'
    assert extract_inpock_hubs(cap) == ['https://link.inpock.co.kr/2ddou_']


def test_api_redirect_excluded():
    # /api/r/<토큰>은 허브가 아니라 개별 버튼 리다이렉트 → 제외. 허브만 남는다.
    text = ('https://link.inpock.co.kr/2ddou_ '
            'https://link.inpock.co.kr/api/r/laXWp81g_tRTK1oYXoY57nQ '
            'https://link.inpock.co.kr/api/r/8K4hitO-OO5VqguVVf6Bs')
    assert extract_inpock_hubs(text) == ['https://link.inpock.co.kr/2ddou_']


def test_inpk_link_short_domain():
    assert extract_inpock_hubs('https://inpk.link/hello') == ['https://inpk.link/hello']


def test_dedup_and_order_across_texts():
    cap = 'https://link.inpock.co.kr/aaa 및 https://link.inpock.co.kr/bbb'
    bio = 'https://link.inpock.co.kr/aaa'   # 캡션과 중복
    assert extract_inpock_hubs(cap, bio) == [
        'https://link.inpock.co.kr/aaa', 'https://link.inpock.co.kr/bbb']


def test_no_inpock_returns_empty():
    assert extract_inpock_hubs('그냥 텍스트 https://smartstore.naver.com/x', '') == []


def test_none_and_empty_inputs_safe():
    assert extract_inpock_hubs(None, '', None) == []


def test_date_normalization():
    import datetime
    assert _date(datetime.datetime(2026, 8, 6, 9, 0)) == '2026-08-06'
    assert _date(datetime.date(2026, 8, 6)) == '2026-08-06'
    assert _date(None) == 'unknown'
    assert _date('') == 'unknown'
