"""코드 검증 게이트 — LLM#5 답과 코드 추출값(facts)을 합쳐 DB에 넣을 최종 필드를 만든다.

원칙:
- 코드가 구조화 데이터에서 찾은 값(가격/할인율/배송)은 항상 LLM 답보다 우선한다.
  단 하나의 예외: 공구 종료 후 페이지 가격이 평시가로 돌아간 경우를 위해, LLM이 캡션에서
  읽은 공구가는 "그 숫자가 캡션에 실제로 존재할 때만" sale_price로 채택한다(존재성 검증).
- LLM이 낸 숫자는 입력 텍스트(캡션+페이지 본문)에 그 숫자가 실제로 있어야 통과 —
  없으면 그 필드만 NULL(환각 차단). 콤마 유무는 무시하고 비교한다.
- 할인율/절약액: 페이지 표기값 우선, 없으면 정가·판매가로 역산(DDL 주석 규칙 그대로).
- 문자열은 DDL 컬럼 폭으로 자르고, 키워드는 최대 5개·쉼표 결합·300자 컷.
"""
import re

# DDL 컬럼 폭
_CAPS = {'brand_name_kr': 100, 'brand_name_en': 150, 'search_keywords': 300,
         'shipping_note': 200, 'composition_info': 300, 'gift_info': 300,
         'coupon_info': 300, 'ai_summary': 1000, 'category': 30, 'subcategory': 50}

# 정가/판매가 비율 상한(sanity) — 이걸 넘으면 정가를 오추출로 보고 버린다. 실제 공구
# 과장 정가는 6~7배까지 관측되므로(뷰티 등) 넉넉히 20배로 잡아 명백한 이상치만 거른다.
MAX_PRICE_MULTIPLE = 20


def _cap(s, n):
    if s is None:
        return None
    s = str(s).strip()
    return s[:n] if s else None


def _to_int(v):
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    d = re.sub(r'[^\d]', '', str(v))
    return int(d) if d else None


def number_in_text(n, haystack_digits):
    """숫자 n이 입력 텍스트에 실제로 표기되어 있는가 — 콤마를 제거한 텍스트에서 부분 문자열로
    찾되, 앞뒤가 숫자가 아닌 경계만 인정한다(12900이 '112900원'에 우연히 매칭되지 않게)."""
    if n is None:
        return False
    return re.search(rf'(?<!\d){n}(?!\d)', haystack_digits) is not None


def _digits_haystack(*texts):
    return re.sub(r'(?<=\d),(?=\d)', '', ' '.join(t or '' for t in texts))


def _keywords(raw):
    """LLM의 키워드 배열 → 쉼표 결합 문자열. 5개 초과는 자르고, 빈/중복 제거. 하나도 안
    남으면 NULL(프롬프트가 5개를 요구하지만 모자라게 온 것을 버리진 않는다 — 검색 보조
    필드라 3~4개라도 있는 게 NULL보다 낫다)."""
    if not isinstance(raw, list):
        return None
    seen, out = set(), []
    for k in raw:
        k = str(k or '').strip().replace(',', ' ').strip()
        if k and k not in seen:
            seen.add(k)
            out.append(k)
        if len(out) == 5:
            break
    return _cap(','.join(out), _CAPS['search_keywords']) if out else None


def merge_and_validate(llm, facts, caption, category=None, subcategory=None):
    """LLM#5 답(llm dict|None) + 코드 facts → detail 테이블 필드 dict."""
    llm = llm or {}
    hay = _digits_haystack(caption, facts.get('body_text'))

    # 가격 — 코드값 우선, LLM값은 존재성 검증 통과 시에만 빈자리 채움
    sale = facts.get('sale_price')
    llm_sale = _to_int(llm.get('sale_price'))
    if llm_sale is not None and number_in_text(llm_sale, hay):
        # 캡션 공구가 우선 규칙: 코드값이 있어도 LLM이 다른 값을 골랐고 그 값이 캡션에
        # 실제로 있으면 LLM 쪽을 믿는다(종료 공구의 평시가 복귀 케이스). 캡션에 없고
        # 페이지에만 있는 값이면 코드값 유지.
        caption_hay = _digits_haystack(caption)
        if sale is None or (llm_sale != sale and number_in_text(llm_sale, caption_hay)):
            sale = llm_sale
    original = facts.get('original_price')
    llm_original = _to_int(llm.get('original_price'))
    if original is None and llm_original is not None and number_in_text(llm_original, hay):
        original = llm_original
    if original is not None and sale is not None and original < sale:
        original = None  # 정가 < 판매가는 추출 오류 — 보수적으로 정가만 버린다
    # 정가가 판매가의 MAX_PRICE_MULTIPLE배를 넘으면 오추출로 본다(실측 2026-08-06 —
    # 그로우러닝 유아교구: sale 35,000 / original 9,900,000(=페이지의 무관한 숫자를 정가로
    # 오인) → 할인율 99%. 화장품 등 과장 정가(6~7배)는 실제로 있으므로 임계는 넉넉히 20배).
    if original is not None and sale and original > sale * MAX_PRICE_MULTIPLE:
        original = None

    # 할인율/절약액 — 페이지 표기(코드) > LLM 표기(존재성 검증) > 역산
    rate = facts.get('discount_rate')
    if rate is None:
        r = _to_int(llm.get('discount_rate'))
        if r is not None and number_in_text(r, hay):
            rate = r
    amount = _to_int(llm.get('discount_amount'))
    if amount is not None and not number_in_text(amount, hay):
        amount = None
    if original and sale and original > sale:
        if rate is None:
            rate = round((original - sale) / original * 100)
        if amount is None:
            amount = original - sale
    rate = rate if (rate is not None and 0 <= rate <= 100) else None

    # 배송 — 코드(구조화/테이블 파서) 우선, LLM은 빈자리만
    free = facts.get('free_shipping')
    if free is None and llm.get('free_shipping') in (0, 1):
        free = llm['free_shipping']
    fee = facts.get('shipping_fee')
    if fee is None:
        f = _to_int(llm.get('shipping_fee'))
        if f is not None and (f == 0 or number_in_text(f, hay)):
            fee = f
    if fee == 0 and free is None:
        free = 1
    note = facts.get('shipping_note') or llm.get('shipping_note')

    conf = _to_int(llm.get('ai_summary_confidence'))
    conf = conf if (conf is not None and 0 <= conf <= 100) else None

    return {
        'brand_name_kr': _cap(llm.get('brand_name_kr') or facts.get('brand'), _CAPS['brand_name_kr']),
        'brand_name_en': _cap(llm.get('brand_name_en'), _CAPS['brand_name_en']),
        'category': _cap(category, _CAPS['category']),
        'subcategory': _cap(subcategory, _CAPS['subcategory']),
        'search_keywords': _keywords(llm.get('search_keywords')),
        'original_price': original,
        'sale_price': sale,
        'discount_rate': rate,
        'discount_amount': amount,
        'free_shipping': free,
        'shipping_fee': fee,
        'shipping_note': _cap(note, _CAPS['shipping_note']),
        'composition_info': _cap(llm.get('composition_info'), _CAPS['composition_info']),
        'gift_info': _cap(llm.get('gift_info'), _CAPS['gift_info']),
        'coupon_info': _cap(llm.get('coupon_info'), _CAPS['coupon_info']),
        'ai_summary': _cap(llm.get('ai_summary'), _CAPS['ai_summary']),
        'ai_summary_confidence': conf,
    }
