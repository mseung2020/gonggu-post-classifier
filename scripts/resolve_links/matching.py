"""후보 링크/상품이 "지금 찾는 그 상품"과 맞는지 판단하는 데 쓰이는 텍스트 휴리스틱들."""
import re


def hint_is_vague(name):
    """product_name이 "OO마켓 상품"/"OO샵 신상품"처럼 특정 상품명이 아니라 스토어명+일반명사뿐이면,
    스토어메인의 카탈로그를 거쳐 고른 아무 상품이나 "일치"로 통과시켜버릴 위험이 있다 — 이런 경우는
    done으로 자동 확정하지 않고 사람이 보게 hold로 돌린다."""
    h = (name or '').strip()
    return bool(re.match(r'^\S+\s*(마켓|샵|스토어|몰|숍)\s*(상품|제품|아이템)$', h))


def post_context_text(product, parent):
    """⚠ classification_note는 포스트 전체 공통 설명이라, 한 포스트가 여러 상품으로 쪼개진
    경우 다른 상품(형제 상품) 이름까지 같이 언급하고 있을 수 있다(실측 확인, 2026-07-21 —
    "3가지 주방템(설거지통,후드필터,다이닝팬)"이 언급되자 LLM이 설거지통·후드필터 요청에도
    다이닝팬 링크를 "같은 캠페인이니 관련있다"고 느슨하게 매칭해버림). 그래서 지금 찾는
    상품명을 명시적으로 못박아 형제 상품과 혼동하지 않게 한다."""
    name = product.get('product_name') or ''
    parts = [name]
    note = parent.get('classification_note')
    if note:
        parts.append(f'(참고: {note} — 단, 지금 찾는 상품은 정확히 "{name}"뿐이며, 이 참고 '
                      f'설명에 다른 상품명이 같이 언급되어도 그건 매칭 대상이 아님)')
    return ' '.join(p for p in parts if p)


def product_key(platform, parent, sort_order):
    native_id = parent.get('post_id') if platform == 'ig' else parent.get('video_id')
    return f'{platform}:{native_id}:{sort_order}'
