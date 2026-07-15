#!/usr/bin/env python3
"""3단계: classified.json에 "확실한 공구만 보수적으로" 게이트를 적용하고,
gonggu_video/gonggu_video_product 또는 gonggu_post/gonggu_post_product 컬럼에 그대로
매핑되는 형태로 정리한다. 크롤링/링크 최종 확정은 이 스크립트의 책임이 아님 — candidate_url은
LLM이 상품별로 뽑은 원본 후보를 그대로 세미콜론으로 이어붙여 참고용으로만 넘긴다.

사용법:
    python3 scripts/transform.py
결과: data/output/load_ready.json — [{platform, parent: {...}, products: [{...}, ...]}]
    + 사유별 제외 건수 출력
"""
from collections import Counter

from common import CLASSIFIED_FILE, LOAD_READY_FILE, dump_json, is_affiliate_ranking, load_json

VALID_LINK_LOCATIONS = {'설명_직접링크', '설명_프로필안내', '댓글참여_DM', '고정댓글_더보기', '링크없음_불명'}


def _valid_date(s):
    """YYYY-MM-DD 형식만 신뢰. LLM이 null/이상한 값을 주면 None."""
    if not s:
        return None
    s = str(s)[:10]
    try:
        y, m, d = map(int, s.split('-'))
        assert 1 <= m <= 12 and 1 <= d <= 31
        return s
    except Exception:
        return None


def _product_row(p, sort_order):
    loc = p.get('link_location')
    if loc not in VALID_LINK_LOCATIONS:
        loc = '링크없음_불명'
    urls = [u for u in (p.get('urls') or []) if u]
    return {
        'product_name': (p.get('name') or '').strip(),
        'link_location': loc,
        'url_type': p.get('url_type') if p.get('url_type') and p.get('url_type') != '없음' else None,
        'candidate_url': ';'.join(urls)[:500] if urls else None,
        'sort_order': sort_order,
    }


def transform_one(post):
    """(parent_row, product_rows, reject_reason) 튜플. reject_reason이 있으면 제외."""
    lc = post.get('classification')
    if post.get('classification_error') or not lc:
        return None, None, f'분류실패: {post.get("classification_error") or "결과 없음"}'

    if not lc.get('is_gonggu'):
        return None, None, 'is_gonggu=false'

    raw_products = [p for p in (lc.get('products') or []) if p and (p.get('name') or '').strip()]
    if not raw_products:
        return None, None, 'products 배열 비어있음(is_gonggu=true인데 상품 특정 실패)'

    all_urls = [u for p in raw_products for u in (p.get('urls') or [])]
    if is_affiliate_ranking(post.get('description'), all_urls):
        return None, None, '제휴 광고성 다중 링크(TOP N 리뷰)'

    product_rows = [_product_row(p, i) for i, p in enumerate(raw_products)]

    gonggu_start = _valid_date(lc.get('period_start'))
    gonggu_end = _valid_date(lc.get('period_end'))
    note = (lc.get('pattern_note') or '').strip()[:500] or None

    if post['platform'] == 'ig':
        parent = {
            'post_id': post['post_id'],
            'user_id': post['user_id'],
            'url': post.get('url'),
            'publish_date': post['publish_date'],
            'gonggu_start_date': gonggu_start,
            'gonggu_end_date': gonggu_end,
            'classification_note': note,
        }
    else:
        parent = {
            'video_id': post['video_id'],
            'channel_id': post['channel_id'],
            'title': post.get('title'),
            'video_url': post.get('video_url'),
            'publishDate': post['publishDate'],
            'gonggu_start_date': gonggu_start,
            'gonggu_end_date': gonggu_end,
            'classification_note': note,
        }
    return parent, product_rows, None


def main():
    posts = load_json(CLASSIFIED_FILE)
    accepted = []
    reasons = Counter()

    for post in posts:
        parent, products, reject_reason = transform_one(post)
        if reject_reason:
            reasons[reject_reason.split('(')[0].split(':')[0].strip()] += 1
            continue
        accepted.append({'platform': post['platform'], 'parent': parent, 'products': products})

    dump_json(LOAD_READY_FILE, accepted)
    ig_n = sum(1 for a in accepted if a['platform'] == 'ig')
    yt_n = sum(1 for a in accepted if a['platform'] == 'yt')
    print(f'전체 {len(posts)}건 중 확정 공구 {len(accepted)}건(ig {ig_n} / yt {yt_n}) -> {LOAD_READY_FILE}')
    print('제외 사유:')
    for reason, n in reasons.most_common():
        print(f'  {n:4d}  {reason}')


if __name__ == '__main__':
    main()
