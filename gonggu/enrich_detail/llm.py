"""LLM 호출 2종 — LLM#5(상세 요약, 신규)와 LLM#4(카테고리, 기존 재사용).

LLM#5는 상품당 1회: 캡션+페이지 본문+코드 추출 참고값을 한 번에 주고 판단 계열 필드
전부를 받는다(같은 입력을 필드별로 여러 번 보내면 비용이 배수로 늘고, 요약과 가격이
서로 다른 얘길 하는 필드 간 불일치도 생긴다). 이미지 URL은 입출력 어디에도 없다.

LLM#4는 classify_category.py의 검증된 캐스케이드(flash 1차 → confidence<0.7만 pro)를
함수째 재사용한다 — Desktop jsonl 배치를 거치지 않고 상품당 직접 호출(대공사 5단계의
"category 편입"이 여기로 흡수됨). 프롬프트/판정 로직은 무수정.
"""
import json

from gonggu.classify_category import classify_one as _classify_category_one
from gonggu.common import call_llm
from gonggu.llm_batch import retry_llm
from gonggu.prompts import DETAIL_ENRICH_SYSTEM, build_detail_enrich_user

from .config import CAPTION_LIMIT, DETAIL_LLM_MODEL


def _facts_line(facts):
    """LLM#5에 '참고값'으로 보여줄 코드 추출 결과 — 이미지 URL은 의도적으로 뺀다."""
    keep = {k: facts.get(k) for k in ('product_name', 'brand', 'sale_price', 'original_price',
                                      'discount_rate', 'shipping_note', 'shipping_fee',
                                      'free_shipping', 'mall_category', 'source')}
    slim = {k: v for k, v in keep.items() if v is not None}
    warn = {
        'list': ('이 페이지는 단일 상품이 아니라 여러 상품이 나열된 목록/카테고리 페이지다 — '
                 '페이지 텍스트의 가격/할인은 입력 상품명과 일치가 확실할 때만 근거로 쓰고, '
                 '아니면 캡션에만 의존할 것'),
        'login-wall': ('이 페이지는 로그인 후에만 상품 정보가 보이는 몰이라 본문이 로그인 안내 '
                       '위주다 — 페이지 텍스트 대신 캡션을 주 근거로 판단할 것'),
        'thin': ('이 페이지는 내용이 거의 없다(캠페인 종료/빈 페이지 가능성) — 캡션을 주 근거로 '
                 '판단하고, 확인 안 되는 필드는 null로 둘 것'),
    }.get(facts.get('page_kind'))
    if warn:
        slim['주의'] = warn
    return json.dumps(slim, ensure_ascii=False) if slim else '(코드 추출값 없음 — 텍스트만으로 판단)'


def call_detail_enrich(product_name, caption, facts, gonggu_stage, publish_date):
    """LLM#5 1회 호출. 반환: (파싱된 dict, None) 또는 (None, 에러 문자열)."""
    user = build_detail_enrich_user(
        product_name=product_name,
        caption=(caption or '')[:CAPTION_LIMIT],
        page_text=facts.get('body_text') or '',
        facts_line=_facts_line(facts),
        gonggu_stage=gonggu_stage,
        publish_date=publish_date,
    )
    return retry_llm(lambda: call_llm(DETAIL_ENRICH_SYSTEM, user, model=DETAIL_LLM_MODEL))


def call_category(product_name, title, caption):
    """LLM#4 — classify_category.classify_one 재사용. 반환: (category, subcategory) —
    실패하면 (None, None)이고 detail 행의 category만 비는 것으로 끝난다(행 전체를
    error로 만들지 않는다 — 카테고리는 부가 정보지 상세 수집의 성패 기준이 아님)."""
    row = {'product_name': product_name or '', 'title': title or '',
           'description': (caption or '')[:CAPTION_LIMIT]}
    try:
        res = _classify_category_one(row)
    except Exception:
        return None, None
    if res.get('classify_error'):
        return None, None
    return res.get('category'), res.get('subcategory')
