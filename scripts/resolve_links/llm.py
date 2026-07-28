"""링크 해석 단계에서 쓰는 LLM 호출 2개 — 판단은 전부 여기로 모은다."""
from common import call_llm
from prompts import (
    LINK_SELECTION_SYSTEM,
    PAGE_JUDGE_SYSTEM,
    build_link_selection_user,
    build_page_judge_user,
)


def pick_link(post_context, candidates):
    """LLM#2 · 공구왕 링크선택 — 링크모음 페이지의 후보 중 하나를 고른다."""
    return call_llm(LINK_SELECTION_SYSTEM, build_link_selection_user(post_context, candidates))


def judge_page(post_context, page_info):
    """LLM#3 · 공구왕 페이지판별 — 도착한 페이지가 최종 상품페이지인지 판별한다."""
    return call_llm(PAGE_JUDGE_SYSTEM, build_page_judge_user(post_context, page_info))
