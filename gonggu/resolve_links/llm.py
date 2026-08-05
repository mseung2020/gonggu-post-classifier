"""링크 해석 단계에서 쓰는 LLM 호출 2개 — 판단은 전부 여기로 모은다.

타임아웃은 LINK_LLM_TIMEOUT(기본 120초 — call_llm 기본값과 동일), LINK_LLM_TIMEOUT_RETRY=1이면
타임아웃에 한해 한 번 재시도한다(4단계 D3, 옵트인 — 근거/주의는 config.py 주석 참고).
"""
import requests

from gonggu.common import call_llm
from gonggu.prompts import (
    LINK_SELECTION_SYSTEM,
    PAGE_JUDGE_SYSTEM,
    build_link_selection_user,
    build_page_judge_user,
)

from .config import LINK_LLM_MODEL, LINK_LLM_TIMEOUT, LINK_LLM_TIMEOUT_RETRY


def _call(system, user):
    attempts = 1 + max(0, LINK_LLM_TIMEOUT_RETRY)
    for i in range(attempts):
        try:
            return call_llm(system, user, timeout=LINK_LLM_TIMEOUT, model=LINK_LLM_MODEL)
        except requests.exceptions.Timeout:
            if i == attempts - 1:
                raise
    raise AssertionError('unreachable')


def pick_link(post_context, candidates):
    """LLM#2 · 공구왕 링크선택 — 링크모음 페이지의 후보 중 하나를 고른다."""
    return _call(LINK_SELECTION_SYSTEM, build_link_selection_user(post_context, candidates))


def judge_page(post_context, page_info):
    """LLM#3 · 공구왕 페이지판별 — 도착한 페이지가 최종 상품페이지인지 판별한다."""
    return _call(PAGE_JUDGE_SYSTEM, build_page_judge_user(post_context, page_info))
