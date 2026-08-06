"""enrich_detail 단위 테스트 — DB/네트워크/LLM 없이 순수 함수만 검증한다.

크롤링·LLM이 낀 통합 동작은 LIMIT 소량 실전 스모크로 확인하는 것이 이 저장소의 규약
(진짜 페이지·진짜 모델 없이는 의미 있는 검증이 안 됨). 여기서는 판정을 좌우하는 결정론
로직 — 배송비 파서, 네이버 preload 파서, JSON-LD/OG 추출, 이미지 정리, 검증 게이트,
SQL 생성 — 을 못박는다.
"""
import json

from gonggu.enrich_detail.extract import extract_facts
from gonggu.enrich_detail.fetchpage import _gone_reason
from gonggu.enrich_detail.images import build_image_rows, collect_detail_images, collect_thumbnails
from gonggu.enrich_detail.naver import extract_naver, load_preloaded_state
from gonggu.enrich_detail.shipping import krw_amount, parse_shipping
from gonggu.enrich_detail.targets import detail_table, fk_col, image_table, select_targets_sql
from gonggu.enrich_detail.validate import merge_and_validate, number_in_text
from gonggu.enrich_detail.writeback import upsert_done_sql, upsert_status_sql
from gonggu.platforms import PLATFORMS


# ── 배송비 파서 ─────────────────────────────────────────────────────────────

def test_shipping_free():
    assert parse_shipping('무료배송') == ('무료배송', 0, None)


def test_shipping_flat_fee():
    note, fee, over = parse_shipping('3,000원')
    assert (fee, over) == (3000, None) and '3,000' in note


def test_shipping_conditional_tiers():
    note, fee, over = parse_shipping('1원 이상 ~ 100,000원 미만 3,000원 / 100,000원 이상 0원')
    assert (fee, over) == (3000, 100000)
    assert '100,000원 이상 무료' in note


def test_shipping_korean_units():
    assert krw_amount('4만원') == 40000
    assert krw_amount('3만5천원') == 35000
    note, fee, over = parse_shipping('기본 배송비 : 3,000원 (4만원 이상 구매 시 무료)')
    assert (fee, over) == (3000, 40000)


# ── 네이버 preload 파서 ──────────────────────────────────────────────────────

def _naver_html():
    state = {'simpleProductForDetailPage': {'A': {
        'name': '테스트 냉감이불 세트',
        'salePrice': 59000,
        'benefitsView': {'discountedSalePrice': 41300, 'discountedRatio': 30},
        'naverShoppingSearchInfo': {'brandName': '쿨브랜드'},
        'productDeliveryInfo': {'deliveryFeeType': 'FREE'},
        'representativeImageUrl': 'https://img.example.com/rep.jpg',
        'optionalImageUrls': ['https://img.example.com/opt1.jpg'],
        'category': {'wholeCategoryName': '가구/인테리어>침구'},
    }}}
    # 실제 preload처럼 JS 리터럴(undefined)이 섞인 형태도 파싱되는지 같이 검증
    raw = json.dumps(state, ensure_ascii=False).replace('"쿨브랜드"}', '"쿨브랜드", "x": undefined}')
    return f'<html><script>window.__PRELOADED_STATE__={raw};</script><body></body></html>'


def test_naver_preload_prices_and_brand():
    facts = extract_naver(_naver_html(), 'https://smartstore.naver.com/x/products/1')
    assert facts['sale_price'] == 41300          # 즉시할인 적용가가 실판매가
    assert facts['original_price'] == 59000      # salePrice는 정가 성격
    assert facts['discount_rate'] == 30 and facts['discount_source'] == 'page'
    assert facts['brand'] == '쿨브랜드'
    assert facts['free_shipping'] == 1 and facts['shipping_fee'] == 0
    assert facts['thumbnail_urls'] == ['https://img.example.com/rep.jpg',
                                       'https://img.example.com/opt1.jpg']


def test_preload_absent_returns_none():
    assert load_preloaded_state('<html><body>no preload</body></html>') is None
    assert extract_naver('<html><body></body></html>', 'https://smartstore.naver.com/x') is None


# ── JSON-LD / OG / Cafe24 ───────────────────────────────────────────────────

def test_jsonld_extraction_min_offer_price():
    html = '''<html><head>
    <script type="application/ld+json">{"@type":"Product","name":"비타민C 세트",
      "brand":{"name":"헬씨"},
      "offers":[{"price":"25,000"},{"price":"19,900"}],
      "image":["https://img.example.com/a.jpg"]}</script>
    </head><body>비타민C 세트 19,900원</body></html>'''
    facts = extract_facts(html, 'https://shop.example.com/p/1', 2000)
    assert facts['product_name'] == '비타민C 세트'
    assert facts['sale_price'] == 19900          # 다중 offer는 최저가가 대표가
    assert facts['brand'] == '헬씨'
    assert facts['thumbnail_urls'] == ['https://img.example.com/a.jpg']
    assert facts['source'] == 'json-ld'


def test_cafe24_spec_table_and_discount():
    html = '''<html><head><meta property="og:title" content="주방 밀폐용기 10종"/></head>
    <body class="xans-product">
    <span class="discount_rate">{#discount_rate}%</span>
    <span class="rate">25%</span>
    <table><caption>기본 정보</caption>
      <tr><th>소비자가</th><td>40,000원</td></tr>
      <tr><th>판매가</th><td>30,000원</td></tr>
      <tr><th>배송비</th><td>3,000원 (50,000원 이상 무료)</td></tr>
    </table>
    <div id="prdDetail"><img ec-data-src="/web/upload/d1.jpg"/><img src="/img/spacer.gif"/></div>
    </body></html>'''
    facts = extract_facts(html, 'https://mall.example.com/product/1', 2000)
    assert facts['solution'] == 'cafe24'
    assert facts['original_price'] == 40000 and facts['sale_price'] == 30000
    assert facts['discount_rate'] == 25 and facts['discount_source'] == 'page'  # 미치환 템플릿({#..})은 건너뜀
    assert facts['shipping_fee'] == 3000 and facts['free_over'] == 50000 and facts['free_shipping'] == 0
    assert facts['detail_image_urls'] == ['https://mall.example.com/web/upload/d1.jpg']  # 스페이서 제외


# ── 이미지 정리 ─────────────────────────────────────────────────────────────

def test_image_cleanup_and_rows():
    thumbs = collect_thumbnails(['//img.x.com/a.jpg', '/rel.jpg', 'data:image/gif;base64,x',
                                 'https://img.x.com/a.jpg'], 'https://shop.x.com/p')
    assert thumbs == ['https://img.x.com/a.jpg', 'https://shop.x.com/rel.jpg']  # 보정+dedupe
    rows = build_image_rows(thumbs, ['https://img.x.com/a.jpg', 'https://img.x.com/d1.jpg'])
    # 썸네일과 겹치는 상세 이미지는 한 번만, sort_order는 타입별 0부터
    assert rows == [('https://img.x.com/a.jpg', 'thumbnail', 0),
                    ('https://shop.x.com/rel.jpg', 'thumbnail', 1),
                    ('https://img.x.com/d1.jpg', 'detail', 0)]


def test_image_url_over_column_width_dropped():
    long_url = 'https://img.x.com/' + 'a' * 600
    assert collect_thumbnails([long_url], 'https://x.com') == []


# ── 검증 게이트 ─────────────────────────────────────────────────────────────

def _base_facts(**over):
    f = {'source': None, 'product_name': None, 'brand': None, 'sale_price': None,
         'original_price': None, 'discount_rate': None, 'discount_source': None,
         'shipping_note': None, 'shipping_fee': None, 'free_shipping': None,
         'free_over': None, 'thumbnail_urls': [], 'detail_image_urls': [],
         'mall_category': None, 'body_text': ''}
    f.update(over)
    return f


def test_validate_hallucinated_price_nulled():
    """LLM이 낸 숫자가 입력 어디에도 없으면 버린다(환각 차단)."""
    llm = {'sale_price': 12345, 'original_price': 99999}
    out = merge_and_validate(llm, _base_facts(body_text='공구가 단돈 9,900원'), caption='')
    assert out['sale_price'] is None and out['original_price'] is None


def test_validate_llm_price_accepted_when_present():
    llm = {'sale_price': 9900}
    out = merge_and_validate(llm, _base_facts(body_text='공구가 단돈 9,900원!'), caption='')
    assert out['sale_price'] == 9900


def test_validate_caption_gonggu_price_beats_page_price():
    """종료 공구: 페이지는 평시가(코드 추출), 캡션에 공구가 명시 → 캡션 값 채택."""
    llm = {'sale_price': 41300}
    facts = _base_facts(sale_price=59000, body_text='판매가 59,000원')
    out = merge_and_validate(llm, facts, caption='공구가 41,300원 (3일 한정)')
    assert out['sale_price'] == 41300


def test_validate_llm_page_price_does_not_override_code():
    """LLM이 고른 값이 캡션엔 없고 페이지에만 있으면 코드 추출값 유지."""
    llm = {'sale_price': 12900}
    facts = _base_facts(sale_price=15900, body_text='특가 12,900원 / 정상가 15,900원')
    out = merge_and_validate(llm, facts, caption='오늘 오픈!')
    assert out['sale_price'] == 15900


def test_validate_discount_derivation():
    facts = _base_facts(sale_price=41300, original_price=59000,
                        body_text='정가 59,000원 공구가 41,300원')
    out = merge_and_validate({}, facts, caption='')
    assert out['discount_rate'] == 30 and out['discount_amount'] == 17700


def test_validate_page_stated_rate_wins_over_calc():
    facts = _base_facts(sale_price=41300, original_price=59000, discount_rate=31,
                        discount_source='page', body_text='31% 할인')
    out = merge_and_validate({}, facts, caption='')
    assert out['discount_rate'] == 31


def test_validate_inverted_prices_drop_original():
    facts = _base_facts(sale_price=50000, original_price=30000, body_text='50,000 30,000')
    out = merge_and_validate({}, facts, caption='')
    assert out['sale_price'] == 50000 and out['original_price'] is None


def test_validate_keywords_capped_at_five_and_joined():
    llm = {'search_keywords': ['냉감이불', '쿨링패드', '냉감이불', '여름침구', '접촉냉감', '침구', '공구']}
    out = merge_and_validate(llm, _base_facts(), caption='')
    assert out['search_keywords'] == '냉감이불,쿨링패드,여름침구,접촉냉감,침구'


def test_validate_free_shipping_from_zero_fee():
    out = merge_and_validate({'shipping_fee': 0}, _base_facts(), caption='')
    assert out['shipping_fee'] == 0 and out['free_shipping'] == 1


def test_validate_length_caps():
    llm = {'ai_summary': '가' * 2000, 'composition_info': '나' * 400}
    out = merge_and_validate(llm, _base_facts(), caption='')
    assert len(out['ai_summary']) == 1000 and len(out['composition_info']) == 300


def test_number_in_text_boundaries():
    hay = '총 112,900원 구성'.replace(',', '')
    assert not number_in_text(12900, hay)   # 112900의 부분 문자열 오탐 금지
    assert number_in_text(112900, hay)


# ── gone 판정 ───────────────────────────────────────────────────────────────

def test_gone_by_status_and_marker():
    assert _gone_reason(404, '') == 'HTTP 404'
    assert _gone_reason(200, '<html>존재하지 않는 상품입니다</html>').startswith('페이지 문구')
    assert _gone_reason(200, '<html>멀쩡한 상품 페이지 품절 임박</html>') is None  # 품절은 gone 아님


# ── 테이블/SQL 파생 ─────────────────────────────────────────────────────────

def test_table_and_fk_derivation():
    ig, yt = PLATFORMS['ig'], PLATFORMS['yt']
    assert detail_table(ig) == 'gonggu_post_product_detail'
    assert image_table(ig) == 'gonggu_post_product_image'
    assert fk_col(ig) == 'post_product_id'
    assert detail_table(yt) == 'gonggu_video_product_detail'
    assert fk_col(yt) == 'video_product_id'


def test_select_sql_targets_done_and_pending_or_error():
    sql = select_targets_sql(PLATFORMS['ig'])
    assert "pp.link_status = 'done'" in sql
    assert "detail_status IN ('pending', 'error')" in sql and 'd.id IS NULL' in sql


def test_upsert_sql_shapes():
    done = upsert_done_sql(PLATFORMS['yt'])
    assert 'gonggu_video_product_detail' in done and 'ON DUPLICATE KEY UPDATE' in done
    assert 'detail_error = NULL' in done                      # done이면 이전 에러 지움
    status = upsert_status_sql(PLATFORMS['ig'])
    assert 'thumbnail_url' not in status                       # 상태 UPSERT는 데이터 필드 불가침
    assert 'detail_status = VALUES(detail_status)' in status


# ── 목록 페이지 가드 (실전 스모크 발견, 2026-08-06) ──────────────────────────

def test_list_url_detection():
    from gonggu.enrich_detail.extract import is_list_url
    assert is_list_url('https://loits.kr/product/list.html?cate_no=280')
    assert is_list_url('https://mall.x.com/shop/shopbrand.html?xcode=1')
    assert not is_list_url('https://smartstore.naver.com/x/products/123')
    assert not is_list_url('https://mall.x.com/product/detail.html?product_no=7')


def test_list_page_drops_product_level_facts():
    """목록 페이지에서는 다른 상품의 가격/할인/이미지가 이 상품 것으로 오귀속되면 안 된다."""
    html = '''<html><body class="xans-product">
    <span class="rate">12%</span>
    <script type="application/ld+json">{"@type":"Product","name":"엉뚱한 상품",
      "offers":{"price":"9900"},"image":"https://img.x.com/other.jpg"}</script>
    <div>목록 텍스트 12% 할인 9,900원</div></body></html>'''
    facts = extract_facts(html, 'https://loits.kr/product/list.html?cate_no=280', 2000)
    assert facts['source'] == 'list-page' and facts['page_kind'] == 'list'
    assert facts['sale_price'] is None and facts['discount_rate'] is None
    assert facts['product_name'] is None and facts['brand'] is None
    assert facts['thumbnail_urls'] == [] and facts['detail_image_urls'] == []
    assert '12%' in facts['body_text']  # 본문은 남김 — LLM이 캡션과 대조할 근거


# ── SPA 하이드레이션/로그인월/빈 페이지 (실전 진단 발견, 2026-08-06) ─────────

def test_needs_hydration_masked_price():
    from gonggu.enrich_detail.fetchpage import _needs_hydration
    spa = '<script>self.__next_f.push([1,"x"])</script><span>00,000원</span>' + 'x' * 4000
    assert _needs_hydration(spa)                       # SPA + 마스킹 가격 → 브라우저 필요
    assert not _needs_hydration('<span>00,000원</span>')   # SPA 마커 없으면 그대로
    assert not _needs_hydration('<script>self.__next_f.push([1,"x"])</script><b>19,900원</b>')


def test_login_wall_and_thin_page_kind():
    login_html = ('<html><body>' + '로그인이 필요합니다. Login Join us 장바구니 주문배송 ' * 20
                  + '</body></html>')
    facts = extract_facts(login_html, 'https://the-arctic.co.kr/?pn=market', 2000)
    assert facts['page_kind'] == 'login-wall'
    thin_html = '<html><body>지혜 로그인 진행 중인 캠페인이 없습니다.</body></html>'
    facts = extract_facts(thin_html, 'https://yestravel.co.kr/i/x', 2000)
    assert facts['page_kind'] == 'thin'


def test_detail_images_by_url_pattern_fallback():
    """컨테이너 셀렉터가 없는 SPA몰 — URL 패턴으로 상세설명 이미지 식별(vyneherb 케이스)."""
    html = '''<html><body>
    <img src="https://s3.x.com/prod/product_detailed_descriptions/a1.jpg" class="_x1"/>
    <img src="https://s3.x.com/prod/product_detailed_descriptions/a2.jpg" class="_x2"/>
    <img src="https://s3.x.com/prod/review_images/r1.jpg"/>
    <img src="/svgs/flags/flag-kr.svg"/>
    </body></html>'''
    facts = extract_facts(html, 'https://shop.x.com/kr/products/2/y', 2000)
    assert facts['detail_image_urls'] == [
        'https://s3.x.com/prod/product_detailed_descriptions/a1.jpg',
        'https://s3.x.com/prod/product_detailed_descriptions/a2.jpg']  # 리뷰/아이콘 제외


def test_login_redirect_url_marks_login_wall():
    """익명 크롤링 시 로그인 요구 상품은 nid.naver.com으로 리다이렉트 — URL로 로그인월 판정."""
    html = '<html><body>' + '아이디 비밀번호 로그인 상태 유지 QR 코드 로그인 회원가입 고객센터 ' * 10 + '</body></html>'
    facts = extract_facts(html, 'https://nid.naver.com/nidlogin.login?url=https%3A%2F%2Fsmartstore...', 2000)
    assert facts['page_kind'] == 'login-wall'


def test_naver_challenge_marker_spacing_variants():
    """네이버 챌린지 문구는 띄어쓰기 변형이 있다 — 공백 제거 비교로 전부 잡아야 한다."""
    from gonggu.enrich_detail.naver_uc import looks_challenged
    assert looks_challenged('NAVER 보안 확인을 완료해 주세요.')      # 띄어쓰기 변형(실측)
    assert looks_challenged('보안확인을 완료해 주세요')               # 붙여쓰기
    assert looks_challenged('현재 서비스 접속이 불가합니다.')
    assert not looks_challenged('보안 인증서가 적용된 안전한 상품 페이지입니다')


def test_validate_absurd_original_price_dropped():
    """정가가 판매가의 20배 초과면 오추출 — 정가/할인율/절약액 전부 무효(그로우러닝 990만원 사고)."""
    facts = _base_facts(sale_price=35000, original_price=9900000,
                        body_text='특가 35,000원 문의 02-9900000')
    out = merge_and_validate({}, facts, caption='')
    assert out['sale_price'] == 35000
    assert out['original_price'] is None
    assert out['discount_rate'] is None and out['discount_amount'] is None


def test_validate_high_but_plausible_multiple_kept():
    """6~7배 과장 정가(뷰티 공구에서 실제 관측)는 유지 — 임계 20배 미만."""
    facts = _base_facts(sale_price=38000, original_price=256000,
                        body_text='정상가 256,000원 공구가 38,000원')
    out = merge_and_validate({}, facts, caption='')
    assert out['original_price'] == 256000 and out['discount_rate'] == 85


def test_nextjs_rsc_price_fallback():
    """SPA 자사몰(vyneherb류): og만 있고 가격 빈 상태 → RSC의 list/retail_price 폴백."""
    html = (r'<html><head><meta property="og:title" content="여리차 키트"/></head><body>'
            r'<script>self.__next_f.push([1,"x\",\"list_price\":110000,\"retail_price\":99000,\"insurance"])</script>'
            r'<script>self.__next_f.push([1,"y\",\"list_price\":220000,\"retail_price\":187000,\"z"])</script>'
            r'</body></html>')
    facts = extract_facts(html, 'https://www.vyneherb.co.kr/kr/products/2/x', 2000)
    assert facts['sale_price'] == 99000 and facts['original_price'] == 110000  # 최저 판매가 옵션
    assert facts['source'] in ('opengraph', 'nextjs-rsc')


def test_nextjs_rsc_not_applied_when_price_exists():
    """JSON-LD 등으로 이미 가격이 잡혔으면 RSC 폴백은 건드리지 않는다."""
    html = (r'<html><head><script type="application/ld+json">{"@type":"Product","name":"x",'
            r'"offers":{"price":"50000"}}</script></head><body>'
            r'<script>self.__next_f.push([1,"\"list_price\":110000,\"retail_price\":99000,"])</script>'
            r'50000원</body></html>')
    facts = extract_facts(html, 'https://shop.x.com/p', 2000)
    assert facts['sale_price'] == 50000


def test_srookpay_option_data_price():
    """srookpay: 화면가 0원, 실가격은 _goods_option_data JS의 최저 옵션가."""
    html = ('<html><body><span class="pdt_sprice">0원</span>'
            "<script>_goods_option_data['SMO123'] = { mg_price: 0, "
            'option_stock: [{"data":[{"value1":"5박스","price":79000,"stock":9},'
            '{"value1":"1박스","price":17900,"stock":9,"supply_price":0}]}] };</script>'
            '</body></html>')
    facts = extract_facts(html, 'https://shop.srookpay.com/x/Detail/SMO123', 2000)
    assert facts['sale_price'] == 17900               # 최저 옵션가(supply_price:0은 제외)
    assert facts['source'] == 'srookpay-optdata'


def test_shipping_from_body_text_fallback():
    """Cafe24 테이블이 없고 배송이 본문 텍스트로만 있는 몰 — 라벨 근처를 파싱."""
    html = ('<html><body><script type="application/ld+json">{"@type":"Product",'
            '"name":"x","offers":{"price":"34000"}}</script>'
            '배송정보 3,000원 34,000원 이상 구매 시 무료배송 제주 추가</body></html>')
    facts = extract_facts(html, 'https://shop.x.com/p', 2000)
    assert facts['shipping_fee'] == 3000 and facts['free_over'] == 34000
    assert facts['free_shipping'] == 0


def test_gmarket_dom_price():
    """G마켓: JSON-LD엔 가격 없고 .price_real DOM에 있음. 기존가/할인률은 price_innerwrap 텍스트."""
    html = ('<html><head><script type="application/ld+json">{"@type":"Product","name":"청독필 세트"}'
            '</script></head><body>'
            '<span class="price_real">105,000원</span>'
            '<div class="price_innerwrap">기존가 150,000원 할인률 30%</div>'
            '</body></html>')
    facts = extract_facts(html, 'https://item.gmarket.co.kr/Item?goodscode=1', 2000)
    assert facts['sale_price'] == 105000
    assert facts['original_price'] == 150000
    assert facts['discount_rate'] == 30 and facts['discount_source'] == 'page'
    assert facts['product_name'] == '청독필 세트'


def test_gmarket_soldout_price_null():
    """품절 G마켓은 .price_real이 'SOLD OUT' → 가격 NULL이 정답(페이지에 가격 없음)."""
    html = ('<html><body><span class="price_real">SOLD OUT</span></body></html>')
    facts = extract_facts(html, 'https://item.gmarket.co.kr/Item?goodscode=2', 2000)
    assert facts['sale_price'] is None


def test_gmarket_bot_and_naver_challenge_markers():
    from gonggu.enrich_detail.naver_uc import looks_challenged
    assert looks_challenged('현재 간단한 봇 확인 절차가 진행되고 있습니다')   # G마켓 봇확인
    assert looks_challenged('원활한 서비스 이용을 위한 간단한 확인 안내')
    assert not looks_challenged('이 상품은 간단하게 조립됩니다')             # 오탐 아님


def test_shipping_text_fallback_rejects_junk():
    """배송 라벨 근처에 숫자가 없는 자유문장은 note로 넣지 않는다(G마켓 오탐 방지)."""
    html = ('<html><body><script type="application/ld+json">{"@type":"Product","name":"x",'
            '"offers":{"price":"55000"}}</script>'
            '배송비가 추가되지 않습니다. 결제시 조건에 따라 배송비가 추가</body></html>')
    facts = extract_facts(html, 'https://item.gmarket.co.kr/Item?goodscode=1', 2000)
    assert facts['shipping_note'] is None and facts['shipping_fee'] is None
    assert facts['free_shipping'] is None
