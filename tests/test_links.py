"""links.py — URL 정규화와 후보 필터링 규칙."""
from gonggu.resolve_links.config import MAX_CANDIDATES
from gonggu.resolve_links.links import _filter_link_pairs, normalize_url


class TestNormalizeUrl:
    def test_missing_colon(self):
        assert normalize_url('https//litt.ly/x') == 'https://litt.ly/x'

    def test_no_scheme(self):
        assert normalize_url('litt.ly/x') == 'https://litt.ly/x'

    def test_double_scheme_keeps_last(self):
        assert normalize_url('https://a.example/https://b.example/p') == 'https://b.example/p'

    def test_pc_blog_rewritten_to_mobile(self):
        assert normalize_url('https://blog.naver.com/who/223') == 'https://m.blog.naver.com/who/223'

    def test_empty(self):
        assert normalize_url('') == ''
        assert normalize_url(None) == ''


class TestFilterLinkPairs:
    def test_bad_domains_dropped(self):
        pairs = [('https://forms.gle/a', '신청폼', 'link'),
                 ('https://smartstore.naver.com/x', '구매', 'link')]
        out = _filter_link_pairs(pairs)
        assert [o['href'] for o in out] == ['https://smartstore.naver.com/x']

    def test_non_product_text_dropped(self):
        pairs = [('https://a.example/1', '고객센터 바로가기', 'link'),
                 ('https://a.example/2', '공지사항', 'link'),
                 ('https://a.example/3', '오늘의 공구', 'link')]
        out = _filter_link_pairs(pairs)
        assert [o['href'] for o in out] == ['https://a.example/3']

    def test_non_product_text_matches_ignoring_spaces_and_case(self):
        out = _filter_link_pairs([('https://a.example/1', '고 객 센 터', 'link'),
                                  ('https://a.example/2', 'CS 문의', 'link')])
        assert out == []

    def test_dedup_and_none_href(self):
        pairs = [(None, 'x', 'link'), ('https://a.example/1', 'A', 'link'),
                 ('https://a.example/1', 'A again', 'link')]
        out = _filter_link_pairs(pairs)
        assert len(out) == 1

    def test_cap_at_max_candidates(self):
        pairs = [(f'https://cafe.naver.com/board/{i}', f'글{i}', 'link') for i in range(MAX_CANDIDATES + 50)]
        assert len(_filter_link_pairs(pairs)) == MAX_CANDIDATES

    def test_source_field_preserved(self):
        out = _filter_link_pairs([('https://a.example/1', '상품 9,900원', 'product')])
        assert out[0]['source'] == 'product'
