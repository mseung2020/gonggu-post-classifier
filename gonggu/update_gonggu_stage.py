#!/usr/bin/env python3
"""gonggu_post/gonggu_video의 gonggu_start_date/gonggu_end_date를 오늘 날짜와 비교해서
gonggu_stage를 갱신한다. LLM 재호출 없이 순수 날짜 비교 + UPDATE만 하는 정적 배치라, 서비스처럼
계속 도는 게 아니라 필요할 때(예: 매일 1회 cron) 실행하고 끝나면 된다.

'종료'는 다시 열릴 일이 없는 종결 상태라 조회 대상에서 아예 제외한다 — 그래서 실제로 확인하는
전이는 두 가지뿐이다: 시작전 -> (아직 시작전 / 진행중 / 종료), 진행중 -> (아직 진행중 / 종료).
날짜 비교 로직은 transform.py의 _compute_stage를 그대로 재사용해서 적재 시점 계산과
어긋나지 않게 한다.

사용법:
    python3 scripts/update_gonggu_stage.py
"""
from gonggu.common import connect_dst
from gonggu.platforms import PLATFORMS
from gonggu.transform import _compute_stage

# 테이블명/자연키 컬럼은 platforms.py 메타테이블이 유일한 정의처(2단계 B4).
TABLES = [(p.parent_table, p.id_col) for p in PLATFORMS.values()]


def _update_table(conn, table, id_col):
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {id_col} AS key_id, gonggu_start_date, gonggu_end_date, gonggu_stage "
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
                cur.execute(f'UPDATE {table} SET gonggu_stage = %s WHERE {id_col} = %s',
                            (new_stage, r['key_id']))
                changed += 1
    conn.commit()
    print(f'{table}: 재검토 {len(rows)}건(종료 제외) 중 {changed}건 갱신')


def main():
    conn = connect_dst()
    try:
        for table, id_col in TABLES:
            _update_table(conn, table, id_col)
    finally:
        conn.close()


if __name__ == '__main__':
    main()
