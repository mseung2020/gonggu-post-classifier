"""다중상품 기간 백필의 상품명 매칭 로직 검증 — 링크 자산 보존을 위해 "애매하면 스킵"이
정확히 동작하는지 못박는다(잘못 매칭해 엉뚱한 기간을 넣느니 NULL로 두는 게 낫다)."""
from gonggu._migrate_multiproduct_periods import match_periods


def test_exact_match_both():
    llm = [{'name': '라무르 선크림', 'period_start': '2026-08-04', 'period_end': '2026-08-14'},
           {'name': '마이키즈 이유식', 'period_start': '2026-08-01', 'period_end': '2026-08-15'}]
    db = [{'id': 10, 'product_name': '라무르 선크림'}, {'id': 11, 'product_name': '마이키즈 이유식'}]
    out = match_periods(llm, db)
    assert (10, '2026-08-04', '2026-08-14') in out
    assert (11, '2026-08-01', '2026-08-15') in out


def test_partial_match_unique():
    # DB명이 LLM명의 부분문자열(공백 무시)이고 유일하면 매칭.
    llm = [{'name': '브이롭티 4+1 붓기순삭템', 'period_start': '2026-08-01', 'period_end': None}]
    db = [{'id': 5, 'product_name': '브이롭티'}]
    assert match_periods(llm, db) == [(5, '2026-08-01', None)]


def test_ambiguous_two_candidates_skipped():
    # '쿨매트'가 두 LLM 상품에 다 포함 → 2개 후보 → 애매 → 스킵.
    llm = [{'name': '쿨매트 싱글', 'period_start': '2026-08-01', 'period_end': '2026-08-05'},
           {'name': '쿨매트 더블', 'period_start': '2026-08-02', 'period_end': '2026-08-06'}]
    db = [{'id': 7, 'product_name': '쿨매트'}]
    assert match_periods(llm, db) == []


def test_no_name_match_skipped():
    llm = [{'name': '전혀 다른 상품', 'period_start': '2026-08-01', 'period_end': None}]
    db = [{'id': 1, 'product_name': '원래 상품'}]
    assert match_periods(llm, db) == []


def test_matched_but_no_period_skipped():
    # 이름은 맞아도 기간이 둘 다 없으면 UPDATE할 게 없으니 스킵(불필요한 쓰기 방지).
    llm = [{'name': 'A상품', 'period_start': None, 'period_end': None}]
    db = [{'id': 1, 'product_name': 'A상품'}]
    assert match_periods(llm, db) == []


def test_invalid_date_dropped():
    llm = [{'name': 'A', 'period_start': '미정', 'period_end': '2026-08-10'}]
    db = [{'id': 1, 'product_name': 'A'}]
    assert match_periods(llm, db) == [(1, None, '2026-08-10')]  # 잘못된 시작일은 None으로
