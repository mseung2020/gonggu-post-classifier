#!/usr/bin/env python3
"""쿠팡/알리익스프레스/테무 최종 링크 상품 일괄 제거 — 제휴형 오픈마켓 링크로 확정된
상품 행을 DB에서 지운다. link_status(done/unresolved/hold/NULL)나 gonggu_stage와
무관하게 candidate_url의 호스트만 본다 — 이 세 마켓은 상태를 가려서 볼 이유가 없다
(done이든 unresolved든, 애초에 이 파이프라인이 다루려는 "공구 판매 링크"가 아니라
오픈마켓 중개 링크이기 때문).

부모(gonggu_post/gonggu_video)는 지우지 않는다 — 상품이 0개가 된 포스트/영상이 남더라도
다른 개발팀이 부모 테이블을 참조하고 있을 수 있어, 삭제 범위를 상품 행으로만 좁힌다.

기본은 미리보기(대상 목록만 출력, DB에 아무것도 안 함)다. DELETE는 되돌릴 수 없으므로
실제로 지우려면 --yes를 명시해야 한다.

실행(저장소 루트에서):
    python3 -m gonggu.purge_marketplace_links               # 미리보기만
    python3 -m gonggu.purge_marketplace_links --yes         # 실제 삭제
    python3 -m gonggu.purge_marketplace_links --status done # link_status 좁히기(기본 전체)
"""
import argparse
import collections
import re

from gonggu.common import connect_dst
from gonggu.platforms import PLATFORMS

# 각 마켓의 대표 도메인 + 알려진 서브도메인/단축링크. 호스트가 이 값과 같거나 그 서브도메인이면
# 매치로 본다(예: link.coupang.com, s.click.aliexpress.com).
MARKETPLACE_DOMAINS = {
    '쿠팡': ('coupang.com', 'coupa.ng'),
    '알리익스프레스': ('aliexpress.com', 'aliexpress.us'),
    '테무': ('temu.com',),
}


def host_of(url):
    """URL에서 도메인만 뽑는다. 파싱 실패하면 빈 문자열(unresolved_board.host_of와 동일 규칙)."""
    m = re.match(r'^\s*(?:https?:)?//([^/?#\s]+)', url or '', re.I)
    return (m.group(1) if m else '').lower().replace('www.', '')


def _matches(host, domain):
    return host == domain or host.endswith('.' + domain)


def marketplace_of(candidate_url):
    """candidate_url(세미콜론으로 여러 후보가 이어붙어 있을 수 있음) 안에 이 세 마켓 중
    하나라도 있으면 그 마켓 이름을, 없으면 None을 돌려준다. 후보가 여럿일 때 하나라도
    걸리면 대상으로 본다 — 후보 중 하나가 오픈마켓이면 그 상품 자체가 제휴형 판매라는
    신호로 보는 게 합리적이기 때문."""
    for candidate in (candidate_url or '').split(';'):
        host = host_of(candidate.strip())
        if not host:
            continue
        for name, domains in MARKETPLACE_DOMAINS.items():
            if any(_matches(host, d) for d in domains):
                return name
    return None


def find_targets(conn, statuses=None):
    """플랫폼별 대상 상품 행을 찾는다. statuses가 None/빈 값이면 link_status 무관하게 전부 본다."""
    targets = []
    with conn.cursor() as cur:
        for code, p in PLATFORMS.items():
            sql = (f"SELECT id, {p.id_col} AS native_id, product_name, candidate_url, "
                   f"link_status FROM {p.product_table} "
                   f"WHERE candidate_url IS NOT NULL AND candidate_url != ''")
            if statuses:
                quoted = ', '.join(f"'{s}'" for s in statuses)
                sql += f' AND link_status IN ({quoted})'
            cur.execute(sql)
            for row in cur.fetchall():
                market = marketplace_of(row['candidate_url'])
                if market:
                    targets.append({**row, 'platform': code, 'market': market})
    return targets


def delete_targets(conn, targets):
    """플랫폼별로 id를 모아 한 번씩 DELETE하고 커밋한다. 반환: {platform: 삭제건수}."""
    by_platform = collections.defaultdict(list)
    for t in targets:
        by_platform[t['platform']].append(t['id'])
    deleted = {}
    with conn.cursor() as cur:
        for code, ids in by_platform.items():
            table = PLATFORMS[code].product_table
            placeholders = ', '.join(['%s'] * len(ids))
            cur.execute(f'DELETE FROM {table} WHERE id IN ({placeholders})', ids)
            deleted[code] = cur.rowcount
    conn.commit()
    return deleted


def _print_preview(targets):
    by_market = collections.Counter(t['market'] for t in targets)
    by_status = collections.Counter(t['link_status'] or 'NULL' for t in targets)
    print(f'대상 {len(targets):,}건')
    print('  마켓별: ' + ', '.join(f'{k}={v:,}' for k, v in by_market.most_common()))
    print('  상태별: ' + ', '.join(f'{k}={v:,}' for k, v in by_status.most_common()))
    print()
    for t in targets[:20]:
        print(f"  [{t['platform']}] {t['market']} · {t['link_status'] or 'NULL'} · "
              f"{t['native_id']} · {t['product_name'] or '(상품명없음)'} · "
              f"{(t['candidate_url'] or '')[:80]}")
    if len(targets) > 20:
        print(f'  ... 외 {len(targets) - 20:,}건')


def main():
    ap = argparse.ArgumentParser(
        description='쿠팡/알리익스프레스/테무 최종 링크 상품 일괄 제거(기본 미리보기)')
    ap.add_argument('--status', default='',
                    help='대상 link_status 콤마 구분(기본 전체 — done/unresolved/hold/NULL 무관)')
    ap.add_argument('--yes', action='store_true', help='실제로 DELETE 실행(기본은 미리보기만)')
    args = ap.parse_args()
    statuses = tuple(s.strip() for s in args.status.split(',') if s.strip())

    conn = connect_dst()
    try:
        targets = find_targets(conn, statuses or None)
        if not targets:
            print('대상 없음 — 쿠팡/알리익스프레스/테무 링크로 확정된 상품이 없습니다.')
            return

        _print_preview(targets)

        if not args.yes:
            print('\n미리보기입니다 — 실제로 삭제하려면 --yes를 붙여 다시 실행하세요.')
            return

        deleted = delete_targets(conn, targets)
        print('\n삭제 완료: ' + ', '.join(f'{k}={v:,}건' for k, v in deleted.items()))
    finally:
        conn.close()


if __name__ == '__main__':
    main()
