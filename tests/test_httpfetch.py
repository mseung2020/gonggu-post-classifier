"""httpfetch.py — 패스트패스의 파싱/스니펫 규칙. 2026-08-01 실측 사고 두 건을 박제:
display:none 모달이 본문 앞을 잡아먹던 문제, 가격이 2000자 창 밖에 있던 문제."""
from bs4 import BeautifulSoup

from resolve_links.httpfetch import _snippet, _strip_hidden, extract_jsonld, extract_jsonld_blocks


class TestExtractJsonld:
    def test_product_direct(self):
        blocks = ['{"@type": "Product", "name": "냄비", "image": ["https://img/1.jpg"],'
                  ' "offers": {"price": "59800", "priceCurrency": "KRW"}}']
        out = extract_jsonld_blocks(blocks)
        assert out == {'name': '냄비', 'image': 'https://img/1.jpg', 'price': '59800',
                       'currency': 'KRW'}

    def test_product_inside_graph_and_offers_list(self):
        blocks = ['{"@graph": [{"@type": "WebSite"}, {"@type": ["Thing", "Product"],'
                  ' "name": "세럼", "offers": [{"price": 12000}]}]}']
        out = extract_jsonld_blocks(blocks)
        assert out['name'] == '세럼' and out['price'] == 12000

    def test_invalid_json_skipped(self):
        assert extract_jsonld_blocks(['{broken', '{"@type": "Product", "name": "x"}'])['name'] == 'x'

    def test_no_product(self):
        assert extract_jsonld_blocks(['{"@type": "WebSite"}']) == {}

    def test_html_wrapper(self):
        html = ('<html><script type="application/ld+json">'
                '{"@type": "Product", "name": "팬"}</script></html>')
        assert extract_jsonld(html)['name'] == '팬'


class TestStripHidden:
    def test_display_none_modal_removed(self):
        # 실측 2026-08-01(shop.byulnanmam.com): display:none 모달이 본문 2000자를 잡아먹음
        html = ('<body><div style="display:none">친구 초대 리워드 안내문' + 'x' * 100 + '</div>'
                '<div>상품명 냄비 59,800원</div>'
                '<div style="visibility: hidden">숨김2</div>'
                '<div aria-hidden="true">숨김3</div><script>var x=1</script></body>')
        soup = BeautifulSoup(html, 'lxml')
        _strip_hidden(soup)
        text = soup.get_text(' ', strip=True)
        assert '리워드' not in text and '숨김2' not in text and '숨김3' not in text
        assert '59,800원' in text


class TestSnippet:
    def test_short_text_unchanged(self):
        assert _snippet('짧은 본문') == '짧은 본문'

    def test_price_outside_window_recentered(self):
        # 실측 2026-08-01(foodshop.co.kr): 앞 2000자가 전부 메뉴라 가격이 잘려나감
        text = '메뉴 ' * 1000 + ' 정가 238,000원 공구가 166,600원 ' + '푸터 ' * 200
        out = _snippet(text)
        assert '238,000' in out and len(out) <= 2000 + 10

    def test_no_price_falls_back_to_head(self):
        text = 'a' * 5000
        assert _snippet(text) == 'a' * 2000
