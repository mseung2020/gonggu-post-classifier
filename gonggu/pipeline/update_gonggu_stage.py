#!/usr/bin/env python3
"""gonggu_post_product/gonggu_video_product의 gonggu_start_date/gonggu_end_date를 **지금 시각**과
비교해서 상품별 gonggu_stage를 갱신한다(기간/스테이지 상품 이전, 2026-08-06). LLM 재호출 없이
순수 비교 + UPDATE만 하는 정적 배치라, 서비스처럼 계속 도는 게 아니라 필요할 때
(매일 퀘스트의 첫 단계) 실행하고 끝나면 된다.

기간이 DATETIME으로 확장되면서(2026-08-21) 판정 기준이 "오늘(날짜)"에서 "이 스크립트를 실제로
돌리는 시각"으로 바뀌었다. 그래서 하루 한 번이 아니라 더 자주 돌릴수록 갭이 촘촘히 메워진다 —
예: "오늘 20시 오픈" 공구는 19시 실행에선 '시작전', 21시 실행에서 '진행중'으로 넘어간다.

'종료'는 다시 열릴 일이 없는 종결 상태라 조회 대상에서 아예 제외한다. 날짜 비교 로직은
transform.py의 _compute_stage를 그대로 재사용해서 적재 시점 계산과 어긋나지 않게 한다.

**시작 후 N일 경과 강제 종료**: 캡션에 시작일만 있고 종료일이 없는 공구는 _compute_stage가
영원히 '진행중'을 돌려준다 — 실제로는 끝난 지 오래인데 DB에 진행중으로 남아 rescan 대상까지
계속 잡아먹는다. 그래서 **이 케이스(시작일 있음 + 종료일 NULL)에 한해서만**, 시작일로부터
FORCE_END_AFTER_DAYS(기본 10)일이 지났으면 '종료'로 강제 전환한다. 이때 gonggu_end_date는
**지어내지 않고 NULL 그대로** 둔다("명시적 날짜만 기록, 추측/환각 금지" 원칙 — 종료인데
end_date가 NULL인 행 = 기간 미상으로 추정 종료된 건). 이 규칙은 이 모듈에만 있다 —
transform의 _compute_stage는 그대로라 적재 시점엔 진행중으로 들어올 수 있지만, 매일 퀘스트의
첫 단계인 이 모듈이 하루 안에 정리한다.

⚠ 이력 메모(2026-08-07): 이 강제 종료 규칙은 원래 eb2d128(2026-08-06)에서 도입됐는데, 직후의
기간→상품 이전 리팩터링(c9e8146)이 이 모듈을 상품 단위로 다시 쓰면서 규칙이 딸려 삭제됐다가
여기서 상품 단위 버전으로 복원됐다. transform._compute_stage는 여전히 불변(골든 diff 무풍).

사용법:
    python3 -m gonggu.update_gonggu_stage
    FORCE_END_AFTER_DAYS=14 python3 -m gonggu.update_gonggu_stage   # 경과 일수 조정
    FORCE_END_AFTER_DAYS=0 python3 -m gonggu.update_gonggu_stage    # 강제 종료 규칙 끄기
"""
import datetime
import os

from gonggu.common import connect_dst
from gonggu.platforms import PLATFORMS
from gonggu.transform import _compute_stage, _now_iso

# 기간/스테이지가 상품 단위로 이전됨 — 상품 테이블을 본다. PK는 두 테이블 모두 `id`.
TABLES = [p.product_table for p in PLATFORMS.values()]
FORCE_END_AFTER_DAYS = int(os.environ.get('FORCE_END_AFTER_DAYS', '10'))


def _fmt_dt(v):
    """DB가 준 DATE/DATETIME을 'YYYY-MM-DD HH:MM:SS'(또는 날짜만)로 직렬화한다.
    DATETIME 확장 전(DATE) 데이터나 이미 문자열인 값도 그대로 받아 넘긴다 — 최종 정규화는
    transform._valid_dt가 하므로 여기서는 구분자만 확실히 공백으로 맞춘다."""
    if not v:
        return None
    if isinstance(v, datetime.datetime):
        return v.strftime('%Y-%m-%d %H:%M:%S')
    if isinstance(v, datetime.date):
        return v.isoformat()
    return str(v)


def stage_with_forced_end(start, end):
    """_compute_stage 결과에 '시작 후 N일 경과 강제 종료' 규칙을 얹는다.
    반환: (stage, 강제 종료 여부). 규칙은 정확히 '시작일 있음 + 종료일 없음 + 진행중' 조합에만
    적용된다 — 종료일이 명시된 공구는 그 날짜가 진실이므로 절대 건드리지 않고, 시작 전
    (미래 시작일)도 대상이 아니다."""
    stage = _compute_stage(start, end)
    if FORCE_END_AFTER_DAYS > 0 and stage == '진행중' and start and not end:
        # 경과 일수는 **날짜 단위**로 센다 — 기간이 DATETIME이 되었지만(2026-08-21) 이 규칙은
        # "시작 후 N일쯤 지났으면 사실상 끝났다고 본다"는 어림짐작이라 초 단위 정밀도가
        # 의미가 없다. 날짜만 잘라 비교하면 DATE 시절과 결과가 정확히 같다(회귀 없음).
        today = datetime.date.fromisoformat(_now_iso()[:10])
        started = datetime.date.fromisoformat(str(start)[:10])
        if (today - started).days >= FORCE_END_AFTER_DAYS:
            return '종료', True
    return stage, False


# 재검토 대상 SELECT. 두 팔이다:
#   ① 날짜가 있고 아직 '종료'가 아닌 행 — 원래 목적(오늘 날짜 기준 재계산).
#   ② gonggu_stage가 NULL인 행 — 정상화(2026-08-19 추가).
#
# ②가 왜 필요한가: _compute_stage는 절대 NULL을 안 준다(날짜가 둘 다 없으면 '판단불가'). 그런데
# 실측(2026-08-19)에서 stage가 NULL인 상품이 1380건 있었고, created_at이 전부 2026-07-21~08-07에
# 몰려 있었다 — 기간/스테이지를 상품 단위로 옮기던 시기(2026-08-06)에 남은 잔재로 보인다.
# 그 행들은 어디서도 안 잡혔다:
#   - 여기: `gonggu_stage != '종료'`가 NULL이면 NULL(=거짓)이라 애초에 선택이 안 됐고, 설령
#     고쳐도 두 번째 조건(날짜 둘 중 하나는 있어야 함)에서 또 빠졌다.
#   - backfill_period: stage='판단불가'만 봄  /  rescan_inprogress: stage='진행중'만 봄
# 결과적으로 1380건(그중 unresolved 1032·hold 32)이 어느 단계도 손대지 않는 사각지대에 있었다.
# NULL을 '판단불가'로 정상화하면 backfill_period가 기간을 찾고, 그러면 stage가 제대로 서면서
# rescan까지 이어진다. NULL-안전 비교를 위해 `!=` 대신 `<=>`(NULL-safe equal)의 부정을 쓴다.
_SELECT_SQL = """
SELECT id, gonggu_start_date, gonggu_end_date, gonggu_stage
FROM {table}
WHERE (NOT (gonggu_stage <=> '종료')
       AND (gonggu_start_date IS NOT NULL OR gonggu_end_date IS NOT NULL))
   OR gonggu_stage IS NULL
"""


def _update_table(conn, table):
    with conn.cursor() as cur:
        cur.execute(_SELECT_SQL.format(table=table))
        rows = cur.fetchall()

    changed = 0
    forced = 0
    with conn.cursor() as cur:
        for r in rows:
            # ⚠ .isoformat()을 쓰지 않는다 — 컬럼이 DATETIME이 된 뒤(2026-08-21) pymysql은
            # datetime 객체를 주고 그 .isoformat()은 'T' 구분자('2026-08-01T20:00:00')를 낸다.
            # _compute_stage는 문자열을 사전식으로 비교하므로 'T'(0x54)가 섞이면 공백(0x20)
            # 기준 값들과의 비교가 조용히 뒤집힌다. 항상 공백 구분자로 직렬화한다.
            start = _fmt_dt(r['gonggu_start_date'])
            end = _fmt_dt(r['gonggu_end_date'])
            new_stage, was_forced = stage_with_forced_end(start, end)
            if new_stage != r['gonggu_stage']:
                cur.execute(f'UPDATE {table} SET gonggu_stage = %s WHERE id = %s',
                            (new_stage, r['id']))
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
        for table in TABLES:
            _update_table(conn, table)
    finally:
        conn.close()


if __name__ == '__main__':
    main()
