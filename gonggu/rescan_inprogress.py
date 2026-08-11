#!/usr/bin/env python3
"""5단계 보강: 링크를 아직 못 찾은 상품의 재탐색 — "전환 즉시 + 지수 백오프 + 은퇴" 스케줄
(2026-08-06 재공사).

왜 이렇게 바꿨나: 예전엔 "진행중+unresolved/hold 전체"를 매일 다시 열었는데, 그 풀이 계속
쌓여서(수천 건) 매일 비용이 선형으로 늘었다. 그런데 재시도 가치는 시간이 지날수록 급감한다 —
링크가 채워지는 결정적 순간은 '시작전→진행중' 전환 직후이고(원준님 피드백), "DM으로만 판매"
"후보 전부 다른 상품" 같은 건 몇 번을 다시 열어도 안 바뀐다. 그래서:

  1. 신규 전환(한 번도 재탐색 안 해본 진행중 상품)  → 무조건 당일 재탐색
  2. link_status='error'(크롤링/LLM 기술 실패)      → 스케줄 무시하고 매일 무조건 포함
  3. 그 외(이미 시도했던 unresolved/hold)           → 백오프: 첫 시도 후 1일 → 2일 → 4일 →
     7일 간격으로 총 (1+len(백오프))회까지만. 다 소진하면 은퇴(보류) — link_status가 바뀌기
     전까지 다시 안 건드린다. 기본 백오프로 약 2주(통상 공구 기간)를 커버한다.

상품별 시도 이력은 data/output/rescan_state.jsonl(append-only last-wins, backfill_period와
같은 검증된 체크포인트 패턴)에 남긴다 — 시도 이력 때문에 DB 스키마에 컬럼을 더하지 않는다
(마이그레이션 없이 파일 체크포인트로 끝내는 편이 안전하고, DB엔 확정 결과만 쓴다).
같은 이유로 예전의 updated_at 기반 "오늘 한 번만"(RESCAN_SKIP_TODAY)은 이 스케줄에 흡수되어
제거됐다. 재공사 후 첫 실행은 기존 풀 전체가 "신규"로 잡혀 한 번 크게 돌고, 그 뒤부터
스케줄에 따라 물량이 급감한다.

resolve_links의 실제 판단/크롤링 로직(resolve_product)과 안티봇 대응(domain_gate)을 그대로
재사용하고, 워커 풀 배관은 crawl_pool.py(2단계 B3), 플랫폼별 SQL은 platforms.py(2단계 B4).
결과는 DB(candidate_url/link_status/link_note UPDATE)와 link_resolution.jsonl 양쪽에 반영해
파일과 DB가 같은 진실을 가리키게 유지한다(link_note = 왜 이 상태인지, 상품 이전 2026-08-07로
DB 상품 행에도 남긴다). candidate_url은 성공/실패와 무관하게 항상 대표 URL 1개다(2026-07-29 결정).

사용법:
    python3 -m gonggu.rescan_inprogress             # 스케줄 대상만(신규전환+에러+백오프 도래)
    LIMIT=50 python3 -m gonggu.rescan_inprogress    # 앞에서 50건만(테스트용)
    RESCAN_CONCURRENCY=40 python3 -m gonggu.rescan_inprogress
    RESCAN_FORCE=1 python3 -m gonggu.rescan_inprogress      # 스케줄 무시, 풀 전체 강제 재시도
    RESCAN_BACKOFF_DAYS=1,3,7 python3 -m gonggu.rescan_inprogress   # 백오프 간격 조정(일)

RESCAN_CONCURRENCY는 "동시에 처리 중인 상품 수"지 "동시에 뜨는 크롬 수"가 아니다 — 실제
브라우저 개수는 MAX_BROWSERS가 따로 제한한다(crawl_pool/browser.py 참고).
"""
import datetime
import os
import sys
from collections import Counter

from gonggu.common import DEEPSEEK_KEY, ROOT, acquire_lock, append_jsonl, connect_dst, load_jsonl
from gonggu.crawl_pool import run_crawl_pool
from gonggu.platforms import PLATFORMS, parent_ctx_from_row, product_update_link_sql
from gonggu.resolve_links.config import HTTP_FAST_PATH, ITEM_DELAY, ITEM_DELAY_SMART, \
    MAX_BROWSERS, RESOLUTION_FILE
from gonggu.resolve_links.core import resolve_product
from gonggu.resolve_links.httpfetch import stats as httpfetch_stats
from gonggu.resolve_links.matching import product_key

RESCAN_CONCURRENCY = int(os.environ.get('RESCAN_CONCURRENCY', '4'))
RESCAN_FORCE = os.environ.get('RESCAN_FORCE', '0') == '1'
# 첫 시도(전환 당일) 이후의 재시도 간격(일). 기본 1,2,4,7 → 상품당 최대 5회, 약 2주 커버.
BACKOFF_DAYS = [int(d) for d in os.environ.get('RESCAN_BACKOFF_DAYS', '1,2,4,7').split(',') if d.strip()]
STATE_FILE = ROOT / 'data/output/rescan_state.jsonl'


def next_due(attempts, today):
    """attempts번째 시도를 마친 직후의 다음 예정일(ISO). 백오프를 다 썼으면 None(은퇴)."""
    if attempts - 1 < len(BACKOFF_DAYS):
        return (today + datetime.timedelta(days=BACKOFF_DAYS[attempts - 1])).isoformat()
    return None


def classify_target(status, rec, today_iso, force=False):
    """이 상품을 이번 실행 대상에 넣을지 판단. 반환: (due 여부, 사유 라벨).

    - error는 무조건 포함(기술 실패는 사업 신호를 기다릴 이유가 없음).
    - 이력 없음 = 진행중이 된 뒤 한 번도 재탐색 안 해봄 → 신규전환, 무조건 포함.
      (재탐색 이력은 이 스크립트가 실제로 시도했을 때만 생기므로, update_gonggu_stage가
      오늘 '진행중'으로 넘긴 새 상품은 자동으로 이 분기에 들어온다.)
    - next_due가 지났으면 백오프 도래, 남았으면 쿨다운, None이면 은퇴(보류)."""
    if status == 'error':
        return True, '에러(무조건)'
    if force:
        return True, '강제(RESCAN_FORCE)'
    if rec is None:
        return True, '신규전환'
    nd = rec.get('next_due')
    if nd is None:
        return False, '은퇴(백오프 소진)'
    if today_iso >= nd:
        return True, '백오프 도래'
    return False, '쿨다운 대기'


def _select_sql(p):
    """재탐색 후보 SELECT — 테이블/컬럼명은 platforms.py 메타에서(2단계 B4). 스케줄 필터링은
    파이썬(체크포인트)에서 하므로 SQL은 후보 풀 전체를 가져온다(SELECT 자체는 싸다 —
    비싼 건 크롤링이고, 그건 스케줄이 줄여준다)."""
    parent_cols = ', '.join(f'p.{c}' for c in p.parent_ctx_cols)
    # 진행중 판정을 상품(pp) 기준으로 — 기간/스테이지가 상품 단위로 이전됨(2026-08-06).
    # 예고 달력처럼 같은 포스트라도 상품마다 진행 상태가 다르므로 상품 stage로 골라야 정확하다.
    return f"""
SELECT pp.id AS row_id, pp.product_name, pp.link_location, pp.url_type, pp.candidate_url,
       pp.sort_order, pp.link_status, {parent_cols}, p.classification_note
FROM {p.product_table} pp
JOIN {p.parent_table} p ON p.{p.id_col} = pp.{p.id_col}
WHERE (pp.gonggu_stage = '진행중' AND pp.link_status IN ('unresolved', 'hold'))
   OR pp.link_status = 'error'
"""


# updated_at을 NOW()로 직접 강제 갱신하는 이유(2026-08-05): MySQL은 값이 하나도 안 바뀌면
# ON UPDATE 트리거를 안 태운다. SQL 생성은 platforms.product_update_link_sql.
UPDATE_SQL = {code: product_update_link_sql(p) for code, p in PLATFORMS.items()}


def _fetch_candidates(conn):
    out = []
    with conn.cursor() as cur:
        for code, p in PLATFORMS.items():
            cur.execute(_select_sql(p))
            for r in cur.fetchall():
                parent = parent_ctx_from_row(p, r)
                product = {'product_name': r['product_name'], 'link_location': r['link_location'],
                           'url_type': r['url_type'], 'candidate_url': r['candidate_url'],
                           'sort_order': r['sort_order']}
                out.append((code, parent, product, r['row_id'], r['link_status']))
    return out


def main():
    acquire_lock('rescan_inprogress')
    if not DEEPSEEK_KEY:
        print('.env에 DEEPSEEK_KEY가 필요합니다.', file=sys.stderr)
        sys.exit(1)

    conn = connect_dst()
    try:
        candidates = _fetch_candidates(conn)
    finally:
        conn.close()

    state = load_jsonl(STATE_FILE)
    today = datetime.date.today()
    today_iso = today.isoformat()

    targets, reasons = [], Counter()
    for code, parent, product, row_id, link_status in candidates:
        key = product_key(code, parent, product['sort_order'])
        due, reason = classify_target(link_status, state.get(key), today_iso, force=RESCAN_FORCE)
        reasons[reason] += 1
        if due:
            targets.append((code, parent, product, row_id, key))

    due_total = len(targets)
    limit = int(os.environ.get('LIMIT', '0')) or due_total
    targets = targets[:limit]

    breakdown = ' / '.join(f'{k} {v}' for k, v in reasons.most_common())
    limit_note = f' (LIMIT으로 {due_total - len(targets)}건 보류)' if len(targets) < due_total else ''
    print(f'재탐색 후보 풀 {len(candidates)}건 → 이번 실행 대상 {len(targets)}건{limit_note}')
    print(f'  분류: {breakdown}')
    if not targets:
        print('  오늘은 재탐색할 것이 없습니다(신규 전환·에러·백오프 도래 건 없음).')
        return

    counters = {}
    print(f'  — 동시 워커 상한 {RESCAN_CONCURRENCY}개, 브라우저 상한 {MAX_BROWSERS}개, '
          f'requests 패스트패스 {"ON" if HTTP_FAST_PATH else "OFF"}, '
          f'백오프 {BACKOFF_DAYS}일 (상품당 최대 {1 + len(BACKOFF_DAYS)}회)')

    def handle(ctx, target):
        code, parent, product, row_id, key = target
        db = ctx.state  # 워커당 DB 커넥션 1개(pymysql 커넥션은 스레드 간 공유가 안전하지 않음)
        try:
            res = resolve_product(ctx.page, code, parent, product)
        except Exception as e:
            res = {'status': 'error', 'final_url': None, 'note': str(e)[:160]}

        # 이 블록에서 뭐가 터지든 이 상품 처리만 실패로 남기고 워커는 죽지 않게 한다(2026-08-04 실측).
        try:
            candidate_url = res.get('candidate_url') or product['candidate_url']
            new_candidate_url = candidate_url[:500] if candidate_url else None

            with ctx.lock:
                # idle 타임아웃으로 끊긴 커넥션 자동 재연결(2026-08-04 실측 사연은 git 이력 참고)
                db.ping(reconnect=True)
                with db.cursor() as cur:
                    note = res.get('note')
                    cur.execute(UPDATE_SQL[code],
                                (new_candidate_url, res['status'], note[:255] if note else None, row_id))
                db.commit()
                append_jsonl(RESOLUTION_FILE, {**res, 'key': key})
                # 스케줄 이력 갱신 — 여전히 못 찾은 상태(unresolved/hold)면 다음 예정일을 잡고,
                # done이 됐거나 error면 next_due는 의미 없음(done은 후보에서 빠지고, error는
                # 스케줄 무시 대상). attempts는 실제 크롤링 시도 횟수의 정직한 기록.
                prev = state.get(key)
                attempts = (prev.get('attempts', 0) if prev else 0) + 1
                rec = {'key': key, 'attempts': attempts, 'checked_at': today_iso,
                       'last_status': res['status'],
                       'next_due': next_due(attempts, today) if res['status'] in ('unresolved', 'hold') else None}
                state[key] = rec
                append_jsonl(STATE_FILE, rec)
                counters[res['status']] = counters.get(res['status'], 0) + 1
                counters['_done_n'] = counters.get('_done_n', 0) + 1
                shown = res.get('final_url') or res.get('note', '')
                print(f"  [{counters['_done_n']}/{len(targets)}] (w{ctx.worker_id}) {key} -> {res['status']} {shown[:70]}",
                      flush=True)
        except Exception as e:
            with ctx.lock:
                counters['error'] = counters.get('error', 0) + 1
                counters['_done_n'] = counters.get('_done_n', 0) + 1
                print(f"  [{counters['_done_n']}/{len(targets)}] (w{ctx.worker_id}) row_id={row_id} -> "
                      f"저장 실패(스킵): {str(e)[:120]}", flush=True)

    run_crawl_pool(targets, handle, concurrency=RESCAN_CONCURRENCY, item_delay=ITEM_DELAY,
                   delay_only_after_browser=ITEM_DELAY_SMART,
                   worker_setup=connect_dst, worker_teardown=lambda db: db.close(),
                   warn_hint='RESCAN_CONCURRENCY')

    hs = httpfetch_stats()
    if hs['tried']:
        print(f"requests 패스트패스: {hs['hit']}/{hs['tried']}건 적중 "
              f"({100 * hs['hit'] / hs['tried']:.1f}%) — 나머지는 브라우저로 폴백")
    by_status = {k: v for k, v in counters.items() if not k.startswith('_')}
    print(f'재탐색 완료 {len(targets)}건 — {by_status}')


if __name__ == '__main__':
    main()
