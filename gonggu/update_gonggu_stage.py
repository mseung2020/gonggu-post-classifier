#!/usr/bin/env python3
"""gonggu_post/gonggu_video의 gonggu_start_date/gonggu_end_date를 오늘 날짜와 비교해서
gonggu_stage를 갱신한다. LLM 재호출 없이 순수 날짜 비교 + UPDATE만 하는 정적 배치라, 서비스처럼
계속 도는 게 아니라 필요할 때(매일 퀘스트의 첫 단계) 실행하고 끝나면 된다.

'종료'는 다시 열릴 일이 없는 종결 상태라 조회 대상에서 아예 제외한다. 날짜 비교 로직은
transform.py의 _compute_stage를 그대로 재사용해서 적재 시점 계산과 어긋나지 않게 한다.

**시작 후 N일 경과 강제 종료(2026-08-06 추가)**: 캡션에 시작일만 있고 종료일이 없는 공구는
_compute_stage가 영원히 '진행중'을 돌려준다 — 실제로는 끝난 지 오래인데 DB에 진행중으로
남아 rescan 대상까지 계속 잡아먹는다. 그래서 **이 케이스(시작일 있음 + 종료일 NULL)에
한해서만**, 시작일로부터 FORCE_END_AFTER_DAYS(기본 10)일이 지났으면 '종료'로 강제 전환한다.
이때 gonggu_end_date는 **지어내지 않고 NULL 그대로** 둔다("명시적 날짜만 기록, 추측/환각
금지" 원칙 — 종료인데 end_date가 NULL인 행 = 기간 미상으로 추정 종료된 건). 이 규칙은
이 모듈에만 있다 — transform의 _compute_stage는 그대로라 적재 시점엔 진행중으로 들어올 수
있지만, 매일 퀘스트의 첫 단계인 이 모듈이 하루 안에 정리한다.

사용법:
    python3 -m gonggu.update_gonggu_stage
    FORCE_END_AFTER_DAYS=14 python3 -m gonggu.update_gonggu_stage   # 경과 일수 조정
    FORCE_END_AFTER_DAYS=0 python3 -m gonggu.update_gonggu_stage    # 강제 종료 규칙 끄기
"""
import datetime
import os

from gonggu.common import connect_dst
from gonggu.platforms import PLATFORMS
from gonggu.transform import _compute_stage, _today_iso

# 테이블명/자연키 컬럼은 platforms.py 메타테이블이 유일한 정의처(2단계 B4).
TABLES = [(p.parent_table, p.id_col) for p in PLATFORMS.values()]
FORCE_END_AFTER_DAYS = int(os.environ.get('FORCE_END_AFTER_DAYS', '10'))


def stage_with_forced_end(start, end):
    """_compute_stage 결과에 '시작 후 N일 경과 강제 종료' 규칙을 얹는다.
    반환: (stage, 강제 종료 여부). 규칙은 정확히 '시작일 있음 + 종료일 없음 + 진행중' 조합에만
    적용된다 — 종료일이 명시된 공구는 그 날짜가 진실이므로 절대 건드리지 않고, 시작 전
    (미래 시작일)도 대상이 아니다."""
    stage = _compute_stage(start, end)
    if FORCE_END_AFTER_DAYS > 0 and stage == '진행중' and start and not end:
        today = datetime.date.fromisoformat(_today_iso())
        started = datetime.date.fromisoformat(str(start)[:10])
        if (today - started).days >= FORCE_END_AFTER_DAYS:
            return '종료', True
    return stage, False


def _update_table(conn, table, id_col):
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {id_col} AS key_id, gonggu_start_date, gonggu_end_date, gonggu_stage "
            f"FROM {table} "
            f"WHERE gonggu_stage != '종료' AND (gonggu_start_date IS NOT NULL OR gonggu_end_date IS NOT NULL)"
        )
        rows = cur.fetchall()

    changed = 0
    forced = 0
    with conn.cursor() as cur:
        for r in rows:
            start = r['gonggu_start_date'].isoformat() if r['gonggu_start_date'] else None
            end = r['gonggu_end_date'].isoformat() if r['gonggu_end_date'] else None
            new_stage, was_forced = stage_with_forced_end(start, end)
            if new_stage != r['gonggu_stage']:
                cur.execute(f'UPDATE {table} SET gonggu_stage = %s WHERE {id_col} = %s',
                            (new_stage, r['key_id']))
                changed += 1
                if was_forced:
                    forced += 1
    conn.commit()
    forced_note = (f' (이 중 시작 {FORCE_END_AFTER_DAYS}일 경과·종료일 없음 강제 종료 {forced}건)'
                   if forced else '')
    print(f'{table}: 재검토 {len(rows)}건(종료 제외) 중 {changed}건 갱신{forced_note}')


def main():
    conn = connect_dst()
    try:
        for table, id_col in TABLES:
            _update_table(conn, table, id_col)
    finally:
        conn.close()


if __name__ == '__main__':
    main()
