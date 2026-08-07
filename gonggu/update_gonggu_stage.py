#!/usr/bin/env python3
"""gonggu_post_product/gonggu_video_product의 gonggu_start_date/gonggu_end_date를 오늘 날짜와
비교해서 상품별 gonggu_stage를 갱신한다(기간/스테이지 상품 이전, 2026-08-06). LLM 재호출 없이
순수 날짜 비교 + UPDATE만 하는 정적 배치라, 서비스처럼 계속 도는 게 아니라 필요할 때
(예: 매일 1회 cron) 실행하고 끝나면 된다.

'종료'는 다시 열릴 일이 없는 종결 상태라 조회 대상에서 아예 제외한다 — 그래서 실제로 확인하는
전이는 두 가지뿐이다: 시작전 -> (아직 시작전 / 진행중 / 종료), 진행중 -> (아직 진행중 / 종료).
날짜 비교 로직은 transform.py의 _compute_stage를 그대로 재사용해서 적재 시점 계산과
어긋나지 않게 한다.

사용법:
    python3 -m gonggu.update_gonggu_stage
"""
from gonggu.common import connect_dst
from gonggu.platforms import PLATFORMS
from gonggu.transform import _compute_stage

# 기간/스테이지가 상품 단위로 이전됨 — 상품 테이블을 본다. PK는 두 테이블 모두 `id`.
TABLES = [p.product_table for p in PLATFORMS.values()]


def _update_table(conn, table):
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT id, gonggu_start_date, gonggu_end_date, gonggu_stage "
            f"FROM {table} "
            f"WHERE gonggu_stage != '종료' AND (gonggu_start_date IS NOT NULL OR gonggu_end_date IS NOT NULL)"
        )
        rows = cur.fetchall()

    changed = 0
    with conn.cursor() as cur:
        for r in rows:
            start = r['gonggu_start_date'].isoformat() if r['gonggu_start_date'] else None
            end = r['gonggu_end_date'].isoformat() if r['gonggu_end_date'] else None
            new_stage = _compute_stage(start, end)
            if new_stage != r['gonggu_stage']:
                cur.execute(f'UPDATE {table} SET gonggu_stage = %s WHERE id = %s',
                            (new_stage, r['id']))
                changed += 1
    conn.commit()
    print(f'{table}: 재검토 {len(rows)}건(종료 제외) 중 {changed}건 갱신')


def main():
    conn = connect_dst()
    try:
        for table in TABLES:
            _update_table(conn, table)
    finally:
        conn.close()


if __name__ == '__main__':
    main()
