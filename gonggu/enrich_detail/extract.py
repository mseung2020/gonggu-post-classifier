"""코드 추출 오케스트레이션 — 크롤링한 HTML에서 결정론적으로 뽑을 수 있는 건 전부 여기서.

계층형 폴백(gonggu_scraper 원리): 네이버 preload(naver.py) → 범용 JSON-LD/OG →
Cafe24 기본정보 테이블 보강 → 상세 이미지. LLM#5는 이 결과(facts)를 "참고값"으로 받아
빈 곳만 채우고, 코드가 찾은 값은 validate.py에서 항상 LLM 답보다 우선한다.

facts dict 키: source, product_name, brand, sale_price, original_price, discount_rate,
discount_source('page'|None), shipping_note, shipping_fee, free_shipping(0/1|None),
free_over, thumbnail_urls[], detail_image_urls[], mall_category, body_text, solution
"""
import json
import re

from bs4 import BeautifulSoup

from .images import collect_detail_images, collect_thumbnails
from .naver import extract_naver
from .shipping import parse_shipping

_PRICE_RE = re.compile(r'\d[\d,]*(?:\.\d+)?')

# 상품 "목록/카테고리" 페이지 URL 패턴 — resolve_links가 목록 페이지를 최종 링크로 확정한
# 경우가 실제로 있다(실전 스모크에서 발견, 2026-08-06 — loits.kr/product/list.html?cate_no=280
# 에서 다른 상품의 할인율 12%가 잡혀 들어옴). 목록 페이지에는 여러 상품의 가격/할인/이미지가
# 섞여 있어 어느 것도 "이 상품의 값"이라고 못 하므로, 가격·할인·이미지 코드 추출을 전부
# 포기하고 NULL로 둔다(캡션에 공구가가 있으면 LLM+존재성 검증 경로로는 여전히 채워짐).
_LIST_URL_RE = re.compile(
    r'(product/list\.html|cate_no=\d+|/goods/goods_list|shopbrand\.html|/category/|mode=search)',
    re.I)


def is_list_url(url):
    return bool(_LIST_URL_RE.search(url or ''))


def _to_int_price(val):
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return int(val)
    m = _PRICE_RE.search(str(val))
    return int(float(m.group(0).replace(',', ''))) if m else None


def _empty_facts():
    return {'source': None, 'product_name': None, 'brand': None, 'sale_price': None,
            'original_price': None, 'discount_rate': None, 'discount_source': None,
            'shipping_note': None, 'shipping_fee': None, 'free_shipping': None,
            'free_over': None, 'thumbnail_urls': [], 'detail_image_urls': [],
            'mall_category': None, 'body_text': '', 'solution': None}


def _iter_jsonld_products(soup):
    for s in soup.find_all('script', attrs={'type': re.compile(r'ld\+json', re.I)}):
        try:
            data = json.loads((s.string or '').strip())
        except Exception:
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                t = node.get('@type')
                types = t if isinstance(t, list) else [t]
                if any(str(x).lower() == 'product' for x in types if x):
                    yield node
                stack.extend(node.values())


def _apply_jsonld(soup, facts):
    for node in _iter_jsonld_products(soup):
        facts['product_name'] = facts['product_name'] or node.get('name')
        brand = node.get('brand')
        if isinstance(brand, dict):
            brand = brand.get('name')
        if isinstance(brand, str) and brand.strip() and not facts['brand']:
            facts['brand'] = brand.strip()
        offers = node.get('offers')
        offer_list = offers if isinstance(offers, list) else [offers] if offers else []
        prices = []
        for off in offer_list:
            if not isinstance(off, dict):
                continue
            ps = off.get('priceSpecification')
            if isinstance(ps, list):
                ps = ps[0] if ps else None
            ps_price = ps.get('price') if isinstance(ps, dict) else None
            p = _to_int_price(off.get('price') or off.get('lowPrice') or ps_price)
            if p:
                prices.append(p)
        if prices and facts['sale_price'] is None:
            facts['sale_price'] = min(prices)  # 다중 offer=옵션 — 대표가는 최저 옵션가
        imgs = node.get('image')
        if imgs:
            imgs = imgs if isinstance(imgs, list) else [imgs]
            facts['thumbnail_urls'].extend(str(i) for i in imgs if i)
        if facts['source'] is None:
            facts['source'] = 'json-ld'


def _meta(soup, prop):
    el = soup.find('meta', attrs={'property': prop}) or soup.find('meta', attrs={'name': prop})
    return el.get('content') if el else None


def _apply_og(soup, facts):
    facts['product_name'] = facts['product_name'] or _meta(soup, 'og:title')
    og_img = _meta(soup, 'og:image')
    if og_img:
        facts['thumbnail_urls'].append(og_img)
    if facts['sale_price'] is None:
        facts['sale_price'] = _to_int_price(_meta(soup, 'product:price:amount'))
    if facts['source'] is None and (facts['product_name'] or facts['sale_price']):
        facts['source'] = 'opengraph'


_LIST_PRICE_LABELS = ('소비자가', '정가', '정상가', '시중가')
_SELL_PRICE_LABELS = ('판매가', '판매가격')
_CAFE24_RE = re.compile(r'(EC-|xans-|/web/product/|cafe24)', re.I)

# Next.js RSC 페이로드(self.__next_f)에 상품 가격이 list_price(정가)/retail_price(판매가)로
# 실리는 SPA 자사몰(실측 2026-08-06: vyneherb.co.kr — shopby 계열). og만 잡히고 가격이 빈
# 경우의 폴백. 이스케이프(\") 상태에서도 숫자는 그대로라 디코딩 없이 정규식으로 뽑는다.
_RSC_PRICE_RE = re.compile(r'list_price\\?":(\d+),\\?"retail_price\\?":(\d+)')


def _apply_nextjs_rsc(html, facts):
    if facts['sale_price'] is not None or 'self.__next_f' not in html:
        return
    cands = [(int(l), int(r)) for l, r in _RSC_PRICE_RE.findall(html)
             if int(r) > 0 and int(l) >= int(r)]
    if not cands:
        return
    list_p, retail_p = min(cands, key=lambda x: x[1])  # 대표가 = 최저 판매가 옵션
    facts['sale_price'] = retail_p
    if list_p > retail_p:
        facts['original_price'] = list_p
    facts['source'] = facts['source'] or 'nextjs-rsc'


# srookpay(shop.srookpay.com 등 결제플랫폼): 가격이 화면엔 옵션 선택 전 '0원'으로 뜨고
# 본문 텍스트에도 실가격이 없다 — 실제 옵션가는 `_goods_option_data['코드'] = {...}` JS 객체의
# option_stock[].data[].price에 임베드돼 있다(정적 fetch로 그대로 옴, 렌더 불필요 — 개발자
# srookpay.py 원리 카피). 우리 정책은 대표 구성 1개이므로 최저 옵션가를 대표 판매가로 쓴다.
_GOODS_OPT_RE = re.compile(r"_goods_option_data\['[^']+'\]\s*=\s*\{")
_OPT_PRICE_RE = re.compile(r'[,{]"price":(\d+)')


def _apply_gmarket(soup, url, facts):
    """G마켓/옥션: JSON-LD엔 이름/이미지만 있고 가격은 DOM에 있다(개발자 gmarket.py 원리).
    .price_real이 판매가, .price_innerwrap 텍스트에 기존가/할인률. 품절이면 .price_real이
    'SOLD OUT'이라 숫자가 없어 자연히 NULL(정상 — 페이지에 가격이 없는 것)."""
    if 'gmarket.co.kr' not in url and 'auction.co.kr' not in url:
        return
    facts['source'] = facts['source'] or 'gmarket'
    if facts['sale_price'] is None:
        real = soup.select_one('.price_real')
        if real:
            facts['sale_price'] = _to_int_price(real.get_text(strip=True))
    wrap = soup.select_one('.price_innerwrap')
    wtxt = wrap.get_text(' ', strip=True) if wrap else ''
    m = re.search(r'기존가\s*([\d,]+)', wtxt)
    if m and facts['original_price'] is None:
        facts['original_price'] = int(m.group(1).replace(',', ''))
    m = re.search(r'할인[률율]\s*(\d{1,3})\s*%', wtxt)
    if m and facts['discount_rate'] is None:
        facts['discount_rate'] = int(m.group(1))
        facts['discount_source'] = 'page'


def _apply_srookpay(html, facts):
    if facts['sale_price'] is not None:
        return
    m = _GOODS_OPT_RE.search(html)
    if not m:
        return
    seg = html[m.start():m.start() + 40000]  # 그 상품의 옵션 블록 범위
    prices = [int(p) for p in _OPT_PRICE_RE.findall(seg) if int(p) > 0]
    if prices:
        facts['sale_price'] = min(prices)   # 최저 옵션가 = 대표 진입가
        facts['source'] = facts['source'] or 'srookpay-optdata'


# 배송비가 Cafe24 '기본 정보' 테이블이 아니라 본문 자유 텍스트로만 있는 몰 대비 폴백.
# '배송비/배송정보' 라벨 근처 200자만 shipping.parse_shipping에 넘긴다(본문 전체를 넘기면
# 다른 숫자에 오탐). 코드가 이미 배송을 잡았으면 건드리지 않는다.
_SHIP_LABEL_RE = re.compile(r'(배송\s*정보|배송비)[^\d]{0,10}(.{0,200})')


def _apply_shipping_from_text(body_text, facts):
    if facts['shipping_note'] is not None or facts['free_shipping'] is not None:
        return
    m = _SHIP_LABEL_RE.search(body_text or '')
    if not m:
        return
    note, fee, over = parse_shipping(m.group(2))
    # 숫자 배송비도 '무료'도 못 찾은 자유문장(예: "배송비가 추가되지 않습니다")은 버린다 —
    # parse_shipping이 원문을 note로 돌려주므로 그대로 넣으면 쓰레기가 들어간다(실측 2026-08-06,
    # G마켓). fee가 정해진 경우(0=무료 포함)만 채택.
    if fee is None:
        return
    facts['shipping_note'] = note
    facts['shipping_fee'] = fee
    facts['free_over'] = facts.get('free_over') or over
    facts['free_shipping'] = 1 if fee == 0 else 0


def _apply_cafe24(soup, html, url, facts):
    """Cafe24 지문이면 '기본 정보' 테이블(th/td)에서 정가/판매가/배송비를 보강하고,
    페이지에 숫자로 적힌 할인율({#...} 미치환 템플릿은 제외)이 있으면 채택한다."""
    if not _CAFE24_RE.search(url + '\n' + html[:20000]):
        return
    facts['solution'] = 'cafe24'
    for tbl in soup.find_all('table'):
        cap = tbl.find('caption')
        if not cap or '기본' not in cap.get_text():
            continue
        for tr in tbl.find_all('tr'):
            th, td = tr.find('th'), tr.find('td')
            if not (th and td):
                continue
            label = th.get_text(strip=True)
            value = td.get_text(' ', strip=True)
            if not label or not value:
                continue
            if '배송비' in label and facts['shipping_note'] is None:
                note, fee, over = parse_shipping(value)
                facts['shipping_note'], facts['shipping_fee'], facts['free_over'] = note, fee, over
                if fee is not None:
                    facts['free_shipping'] = 1 if fee == 0 else 0
            elif label in _LIST_PRICE_LABELS and facts['original_price'] is None:
                facts['original_price'] = _to_int_price(value)
            elif label in _SELL_PRICE_LABELS:
                v = _to_int_price(value)
                if v:
                    facts['sale_price'] = v  # 표값이 JSON-LD보다 최신인 경우가 많아 우선
        break
    if facts['discount_rate'] is None:
        for node in soup.select(".discount_rate, .rate, [class*='discount']"):
            txt = node.get_text(strip=True)
            if not txt or '{' in txt:
                continue
            m = re.search(r'(\d{1,3})\s*%', txt)
            if m:
                facts['discount_rate'] = int(m.group(1))
                facts['discount_source'] = 'page'
                break


_HIDDEN_STYLE = re.compile(r'display\s*:\s*none|visibility\s*:\s*hidden', re.I)


def _body_text(soup, limit):
    """LLM#5에 넘길 본문 — 숨김 요소 제거(httpfetch의 실측 교훈), 가격 표기가 창 밖이면
    그 근처로 창을 옮긴다(_snippet 원리)."""
    for t in soup(['script', 'style', 'noscript', 'template']):
        t.decompose()
    for el in soup.find_all(style=_HIDDEN_STYLE):
        el.decompose()
    body = soup.body or soup
    text = body.get_text(' ', strip=True)
    if len(text) <= limit:
        return text
    m = re.search(r'\d{1,3}(?:,\d{3})+\s*원', text[limit:])
    if not m:
        return text[:limit]
    start = max(0, limit + m.start() - limit // 4)
    return text[start:start + limit]


def extract_facts(html, url, page_text_limit):
    """HTML → facts. 네이버면 preload가 본체, 아니면 JSON-LD/OG → Cafe24 순 폴백.
    목록/카테고리 URL이면 상품 단위 값(가격/할인/이미지)은 오귀속 위험이라 전부 버린다."""
    facts = _empty_facts()
    soup = BeautifulSoup(html, 'lxml')

    nv = extract_naver(html, url)
    if nv:
        facts.update(nv)
    else:
        _apply_jsonld(soup, facts)
        _apply_og(soup, facts)
        _apply_gmarket(soup, url, facts)  # G마켓/옥션 DOM 가격(JSON-LD엔 없음)
        _apply_cafe24(soup, html, url, facts)
        _apply_srookpay(html, facts)    # 결제플랫폼 옵션 JS 폴백
        _apply_nextjs_rsc(html, facts)  # SPA 자사몰 폴백(og만 잡힌 경우)

    if is_list_url(url):
        facts.update({'sale_price': None, 'original_price': None, 'discount_rate': None,
                      'discount_source': None, 'product_name': None, 'brand': None,
                      'thumbnail_urls': [], 'detail_image_urls': [],
                      'source': 'list-page', 'page_kind': 'list'})
        facts['body_text'] = _body_text(soup, page_text_limit)
        return facts

    facts['thumbnail_urls'] = collect_thumbnails(facts['thumbnail_urls'], url)
    facts['detail_image_urls'] = collect_detail_images(
        soup, url, pre_collected=facts['detail_image_urls'] or None)
    facts['body_text'] = _body_text(soup, page_text_limit)
    _apply_shipping_from_text(facts['body_text'], facts)  # 배송 본문 텍스트 폴백
    facts['page_kind'] = facts.get('page_kind') or _page_kind(facts['body_text'], url)
    return facts


# 로그인월 몰(공동구매 상품이 로그인 후에만 보임 — 실측: the-arctic.co.kr, 2026-08-06)과
# 내용이 거의 없는 페이지(캠페인 종료 등 — 실측: yestravel.co.kr) 감지. gone은 아니다 —
# 페이지는 살아있으니 done으로 두되, LLM이 캡션 위주로 판단하도록 참고값에 경고만 붙인다.
_LOGIN_WALL_MARKERS = ('로그인이 필요합니다', '로그인 후 이용', '로그인 후 확인', '회원 전용')
# 로그인 페이지로 리다이렉트된 최종 URL 패턴 — 익명 크롤링(PW_USE_AUTH=0) 시 로그인 요구
# 상품이 네이버 통합로그인(nid.naver.com)으로 넘어가는 케이스(실측 2026-08-06).
_LOGIN_URL_PAT = re.compile(r'(nid\.naver\.com|/login|nidlogin|accounts\.)', re.I)
_THIN_BODY_LEN = 120


def _page_kind(body_text, url=''):
    if _LOGIN_URL_PAT.search(url or ''):
        return 'login-wall'
    head = (body_text or '')[:400]
    if any(m in head for m in _LOGIN_WALL_MARKERS):
        return 'login-wall'
    if len(body_text or '') < _THIN_BODY_LEN:
        return 'thin'
    return None
