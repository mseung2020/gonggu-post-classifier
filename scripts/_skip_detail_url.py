#!/usr/bin/env python3
"""일회성: 특정 URL(부분일치) 상품의 detail_status를 'gone'으로 찍어 상세수집 대상에서 뺀다.

크롬을 매번 터뜨리는 'poison' 상품페이지(예: 초헤비 오늘의집 페이지)를 수동으로 건너뛸 때 쓴다.
읽는 건 candidate_url뿐, 쓰는 건 detail_status/detail_error뿐 — 링크(candidate_url/link_status)나
다른 데이터는 손대지 않는다. 나중에 다시 시도하고 싶으면 그 행의 detail_status를 'blocked'로
되돌리면 uc 대상으로 다시 잡힌다.

사용법(저장소 루트에서):
    python3 -m gonggu._skip_detail_url ohou.se/goods/3924597
    python3 -m gonggu._skip_detail_url 조각1 조각2 ...      # 여러 개 한 번에
"""
import sys

from gonggu.common import connect_dst
from gonggu.enrich_detail.writeback import write_status
from gonggu.platforms import PLATFORMS

NOTE = 'poison URL 수동 스킵(크롬 크래시로 대상에서 제외)'


def main():
    subs = [s for s in sys.argv[1:] if s.strip()]
    if not subs:
        sys.exit('사용법: python3 -m gonggu._skip_detail_url <url조각> [<url조각> ...]')

    conn = connect_dst()
    total = 0
    try:
        for code, p in PLATFORMS.items():
            with conn.cursor() as cur:
                for sub in subs:
                    cur.execute(
                        f"SELECT id, candidate_url FROM {p.product_table} WHERE candidate_url LIKE %s",
                        (f'%{sub}%',))
                    rows = cur.fetchall()
                    for r in rows:
                        write_status(conn, code, r['id'], 'gone', NOTE)
                        total += 1
                        print(f"  스킵: {code} id={r['id']}  {str(r['candidate_url'])[:90]}")
    finally:
        conn.close()

    if total == 0:
        print('해당 URL 조각과 일치하는 상품을 못 찾았습니다(candidate_url 기준).')
    else:
        print(f"완료 — {total}건을 detail_status='gone'으로 제외했습니다. "
              f"이제 crawl_stage를 재시작하면 그 상품(들)은 건너뜁니다.")


if __name__ == '__main__':
    main()
