"""네이버 스마트스토어/브랜드스토어 전용 코드 추출 — __PRELOADED_STATE__ 파싱.

상품페이지 HTML 안 `window.__PRELOADED_STATE__={...}`(JS 객체 리터럴)에 상품 데이터가
들어있다(gonggu_scraper 실측 원리 카피, 2026-08): 핵심 노드는
state["simpleProductForDetailPage"]["A"](채널 키는 가변)이고, 가격은
benefitsView.discountedSalePrice(즉시할인 적용가 = 실제 판매가) vs salePrice(정가 성격),
할인율은 benefitsView.discountedRatio(페이지 표기값), 배송은 productDeliveryInfo,
대표 이미지는 representativeImageUrl + optionalImageUrls. 상세설명 이미지는 preload가
아니라 렌더된 DOM의 SmartEditor(.se-main-container)에 있다.

주의: preload는 순수 JSON이 아니라 undefined/NaN 같은 JS 리터럴을 포함 → 치환 후 파싱.
"""
import json
import re
from urllib.parse import urljoin

_ANCHOR = '__PRELOADED_STATE__'
# 값 위치의 JS 리터럴(undefined/NaN/Infinity)을 null로 → JSON 파싱 가능하게
_JS_LITERAL = re.compile(r'(?<=[:,\[])\s*(?:undefined|NaN|-?Infinity)\s*(?=[,\]}])')


def load_preloaded_state(html):
    """중괄호 균형(+문자열 이스케이프 인식)으로 객체 경계를 찾아 dict로. 없으면 None."""
    i = html.find(_ANCHOR)
    if i < 0:
        return None
    start = html.find('{', i)
    if start < 0:
        return None
    depth, instr, esc, end = 0, False, False, None
    for j in range(start, len(html)):
        c = html[j]
        if instr:
            if esc:
                esc = False
            elif c == '\\':
                esc = True
            elif c == '"':
                instr = False
        else:
            if c == '"':
                instr = True
            elif c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    end = j
                    break
    if end is None:
        return None
    try:
        return json.loads(_JS_LITERAL.sub('null', html[start:end + 1]))
    except json.JSONDecodeError:
        return None


def _find_product_node(state):
    spdp = state.get('simpleProductForDetailPage')
    if isinstance(spdp, dict):
        for v in spdp.values():
            if isinstance(v, dict) and v.get('name'):
                return v
    found = None

    def walk(n):
        nonlocal found
        if found is not None:
            return
        if isinstance(n, dict):
            if n.get('name') and any(n.get(k) for k in ('salePrice', 'dispSalePrice')):
                found = n
                return
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)

    walk(state)
    return found


def _int(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str):
        d = re.sub(r'[^\d]', '', v)
        return int(d) if d else None
    return None


def _shipping(delivery):
    """productDeliveryInfo → (요약문, 배송비, 무료 여부 0/1)."""
    if not isinstance(delivery, dict):
        return None, None, None
    t = delivery.get('deliveryFeeType')
    base = _int(delivery.get('baseFee'))
    if t == 'FREE':
        return '무료배송', 0, 1
    if t == 'CONDITIONAL_FREE':
        over = _int(delivery.get('freeConditionalAmount'))
        note = f'{base or 0:,}원' + (f' ({over:,}원 이상 무료)' if over else ' (조건부 무료)')
        return note, base, (0 if base else 1)
    if base is not None:
        return f'{base:,}원', base, (1 if base == 0 else 0)
    return (str(t) if t else None), None, None


def extract_naver(html, url):
    """성공 시 facts dict, preload가 없으면 None(→ 범용 추출로 폴백)."""
    state = load_preloaded_state(html)
    if not state:
        return None
    n = _find_product_node(state)
    if not n:
        return None

    bv = n.get('benefitsView') if isinstance(n.get('benefitsView'), dict) else {}
    sale = _int(n.get('salePrice') or n.get('dispSalePrice'))   # 정가 성격
    disc = _int(bv.get('discountedSalePrice'))                   # 즉시할인 적용가(실판매가)
    price = disc or sale
    original = sale if (sale and disc and sale > disc) else None
    ratio = _int(bv.get('discountedRatio'))

    ssi = n.get('naverShoppingSearchInfo') if isinstance(n.get('naverShoppingSearchInfo'), dict) else {}
    note, fee, free = _shipping(n.get('productDeliveryInfo'))

    thumbs = []
    if n.get('representativeImageUrl'):
        thumbs.append(n['representativeImageUrl'])
    for u in n.get('optionalImageUrls') or []:
        if u:
            thumbs.append(u)

    cat = n.get('category') if isinstance(n.get('category'), dict) else {}
    return {
        'source': 'naver-preload',
        'product_name': n.get('name'),
        'brand': (ssi.get('brandName') or '').strip() or None,
        'sale_price': price,
        'original_price': original,
        'discount_rate': ratio if ratio else None,   # 페이지 표기값(있을 때만)
        'discount_source': 'page' if ratio else None,
        'shipping_note': note,
        'shipping_fee': fee,
        'free_shipping': free,
        'thumbnail_urls': list(dict.fromkeys(thumbs)),
        'detail_image_urls': smarteditor_images(html, url),
        'mall_category': cat.get('wholeCategoryName') or None,
    }


_IMG_ATTRS = ('data-src', 'data-lazy-src', 'src')


def smarteditor_images(html, url, soup=None):
    """렌더된 DOM의 SmartEditor(.se-main-container) 상세 이미지 URL 수집."""
    from bs4 import BeautifulSoup
    tree = soup or BeautifulSoup(html, 'lxml')
    box = tree.select_one('.se-main-container') or tree.select_one('.se-viewer')
    if box is None:
        return []
    out = []
    for im in box.find_all('img'):
        src = next((im.get(a) for a in _IMG_ATTRS if im.get(a)), None)
        if src and not src.startswith('data:'):
            out.append(urljoin(url, src.strip()))
    return list(dict.fromkeys(out))
