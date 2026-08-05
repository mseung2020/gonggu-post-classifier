"""antibot.py / matching.py — 사고 하나 = 방어 규칙 하나로 대응되는 함수들의 박제."""
from resolve_links.antibot import is_linkbio_hub, is_non_mall, looks_discontinued, recover_from_block
from resolve_links.matching import hint_is_vague, post_context_text, product_key


class TestLooksDiscontinued:
    def test_error_exact_path_flagged(self):
        # 실측 2026-07-20: hi.thehyundai.com/error가 HTTP 200으로 done 확정됐던 사고
        assert looks_discontinued('https://hi.thehyundai.com/error')
        assert looks_discontinued('https://a.example/404/')

    def test_error_substring_in_real_path_not_flagged(self):
        assert not looks_discontinued('https://a.example/error-resistant-widget')

    def test_soldout_marker(self):
        assert looks_discontinued('https://mall.example/item?state=SoldOut')

    def test_normal_product_url(self):
        assert not looks_discontinued('https://smartstore.naver.com/s/products/123')


class TestRecoverFromBlock:
    def test_naver_login_wall_url_param(self):
        url = 'https://nid.naver.com/nidlogin.login?mode=form&url=https%3A%2F%2Fsmartstore.naver.com%2Fs%2Fp%2F1'
        assert recover_from_block(url) == 'https://smartstore.naver.com/s/p/1'

    def test_cloudflare_token_stripped(self):
        url = 'https://item.gmarket.co.kr/Item?goodscode=1&__cf_chl_rt_tk=abc123'
        assert recover_from_block(url) == 'https://item.gmarket.co.kr/Item?goodscode=1'

    def test_unrecoverable(self):
        assert recover_from_block('https://open.kakao.com/o/xyz') is None


class TestHubAndMall:
    def test_linkbio_hub_detected(self):
        # 실측 2026-07-21: 인포크 A의 버튼이 인포크 B를 가리켰는데 done 확정됐던 사고
        assert is_linkbio_hub('https://link.inpock.co.kr/someone')
        assert is_linkbio_hub('https://litt.ly/someone')
        assert not is_linkbio_hub('https://smartstore.naver.com/x')

    def test_naver_blog_is_non_mall(self):
        assert is_non_mall('https://blog.naver.com/who/223')
        assert is_non_mall('https://m.blog.naver.com/who/223')
        assert not is_non_mall('https://smartstore.naver.com/x')


class TestMatching:
    def test_vague_store_name_pattern(self):
        assert hint_is_vague('윤남매맘 마켓 상품')
        assert hint_is_vague('OO샵 신상품') is False  # "신상품"은 패턴 밖(상품/제품/아이템만)
        assert hint_is_vague('스텐 3중 바닥 냄비 24cm') is False

    def test_post_context_pins_exact_product(self):
        # 실측 2026-07-21: 형제 상품(설거지통/후드필터/다이닝팬) 혼동 방지 문구
        ctx = post_context_text({'product_name': '설거지통'},
                                {'classification_note': '3가지 주방템(설거지통,후드필터,다이닝팬)'})
        assert '정확히 "설거지통"' in ctx and '매칭 대상이 아님' in ctx

    def test_product_key(self):
        assert product_key('ig', {'post_id': 'P1'}, 2) == 'ig:P1:2'
        assert product_key('yt', {'video_id': 'V9'}, 0) == 'yt:V9:0'
