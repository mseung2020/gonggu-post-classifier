"""링크 해석 단계에서 쓰는 LLM 호출 2개 — 판단은 전부 여기로 모은다."""
from common import call_dify

from .config import DIFY_KEY_JUDGE, DIFY_KEY_PICK


def pick_link(post_context, candidates):
    """LLM#2 · 공구왕 링크선택 — 링크모음 페이지의 후보 중 하나를 고른다."""
    return call_dify({'post_context': post_context, 'candidates': candidates}, api_key=DIFY_KEY_PICK)


def judge_page(post_context, page_info):
    """LLM#3 · 공구왕 페이지판별 — 도착한 페이지가 최종 상품페이지인지 판별한다."""
    return call_dify({'post_context': post_context, 'page': page_info}, api_key=DIFY_KEY_JUDGE)
