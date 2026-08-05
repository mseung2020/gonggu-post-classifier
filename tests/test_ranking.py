"""ranking.py — 후보 정렬/제거 규칙. 주석에 기록된 실측 사고들을 테스트로 박제."""
from resolve_links.ranking import _dedup_key, handle_matches, rank_candidates


class TestDedup:
    def test_query_string_variants_merge(self):
        # 실측 2026-07-29: ozip.me/ZrEcYOm vs ozip.me/ZrEcYOm?af 는 같은 후보
        assert _dedup_key('https://ozip.me/ZrEcYOm') == _dedup_key('https://ozip.me/ZrEcYOm?af')

    def test_trailing_slash_merges(self):
        assert _dedup_key('https://a.example/x/') == _dedup_key('https://a.example/x')

    def test_different_path_stays_distinct(self):
        # 실측 2026-07-29: 언더바 유무가 서로 다른 계정(kkang_twins vs kkang_twins_)
        assert _dedup_key('https://litt.ly/kkang_twins') != _dedup_key('https://litt.ly/kkang_twins_')


class TestHandleMatch:
    def test_exact_handle_slug(self):
        assert handle_matches('https://my.wiredy.io/pullkim_', 'pullkim_')

    def test_underscore_is_significant(self):
        assert not handle_matches('https://litt.ly/kkang_twins', 'kkang_twins_')

    def test_at_sign_and_case_normalized(self):
        assert handle_matches('https://litt.ly/Viki105', '@viki105')


class TestRankCandidates:
    def test_truncated_and_bad_domains_removed(self):
        urls = ['https://smartstore.naver.com/x/prod...', 'https://forms.gle/abc',
                'https://litt.ly/shop1']
        assert rank_candidates(urls) == ['https://litt.ly/shop1']

    def test_tier_order_hub_mall_ambiguous_nonmall(self):
        urls = ['https://m.blog.naver.com/who/223',        # 비몰(최후 수단)
                'https://unknown-shop.example/item/3',      # 그 외
                'https://smartstore.naver.com/s/p/1',       # 확정몰
                'https://litt.ly/someone']                  # 링크인바이오 허브
        assert rank_candidates(urls) == [
            'https://litt.ly/someone',
            'https://smartstore.naver.com/s/p/1',
            'https://unknown-shop.example/item/3',
            'https://m.blog.naver.com/who/223',
        ]

    def test_handle_match_beats_tier(self):
        # 핸들 일치(모르는 도메인)가 링크인바이오 허브보다도 앞에 온다
        urls = ['https://litt.ly/other_account', 'https://my.wiredy.io/pullkim_']
        assert rank_candidates(urls, handle='pullkim_')[0] == 'https://my.wiredy.io/pullkim_'

    def test_stable_order_within_same_tier(self):
        urls = ['https://a.example/1', 'https://b.example/2']
        assert rank_candidates(urls) == urls
