#!/usr/bin/env python3
"""5단계 보강: 링크를 아직 못 찾은 상품의 재탐색 — "전환 즉시 + 지수 백오프 + 은퇴" 스케줄
(2026-08-06 재공사).

왜 이렇게 바꿨나: 예전엔 "진행중+unresolved/hold 전체"를 매일 다시 열었는데, 그 풀이 계속
쌓여서(수천 건) 매일 비용이 선형으로 늘었다. 그런데 재시도 가치는 시간이 지날수록 급감한다 —
링크가 채워지는 결정적 순간은 '시작전→진행중' 전환 직후이고(원준님 피드백), "DM으로만 판매"
"후보 전부 다른 상품" 같은 건 몇 번을 다시 열어도 안 바뀐다. 그래서:

  1. 신규 전환(한 번도 재탐색 안 해본 진행중 상품)  → 무조건 당일 재탐색
  2. link_status='error'(크롤링/LLM 기술 실패)      → 스케줄(백오프 날짜) 무시하고 매일 포함,
     단 RESCAN_ERROR_MAX_ATTEMPTS(기본 14)번 넘게 계속 error로만 끝나면 은퇴(2026-08-18 추가
     — 원래 상한이 전혀 없어서, 특정 URL 패턴에서 resolve_product가 매번 예외를 던지는 것
     같은 영구적 기술 문제도 매일 무기한 재시도 대상에 남는 문제가 있었다. unresolved/hold의
     "백오프 소진 후 은퇴"와 대칭되는 안전판).
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
    RESCAN_ERROR_MAX_ATTEMPTS=30 python3 -m gonggu.rescan_inprogress   # error 은퇴 상한 조정

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
from gonggu.resolve_links.browser import via_stats
from gonggu.resolve_links.core import resolve_product
from gonggu.resolve_links.httpfetch import body_too_short_samples
from gonggu.resolve_links.httpfetch import stats as httpfetch_stats
from gonggu.resolve_links.matching import product_key
from gonggu.resolve_links.runner import _print_resolution_diagnostics

RESCAN_CONCURRENCY = int(os.environ.get('RESCAN_CONCURRENCY', '4'))
RESCAN_FORCE = os.environ.get('RESCAN_FORCE', '0') == '1'
# 첫 시도(전환 당일) 이후의 재시도 간격(일). 기본 1,2 → 상품당 최대 3회.
#
# ⚠ 2026-08-20 축소(예전 1,2,4,7 → 상품당 5회) — 회차별 성과를 실측하니(이력 8,570건) 3회차부터
# 절벽이었다:
#   1회차 3,200건 중 done 740(23.1%) / 2회차 2,610건 중 316(12.1%)
#   3회차 2,421건 중 done 59(2.4%) / 4회차 326건 중 6(1.8%)
# backfill_period에서 본 것과 같은 모양(1회 17.8% → 2회 0.8% → 3회 0.1%)이라 같은 결론을 적용한다
# — 3·4회차(시도 2,747건)를 없애면 헛시도의 35%를 아끼고 잃는 건 done 65건(2.4%)뿐이다.
#
# 유입이 구조적이라(공구 시작일 기준 하루 400~600건이 꾸준히 진행중으로 전환, 일시적 밀림이
# 아님) 이 축소가 없으면 백오프 파이프라인이 다 차는 정상상태에서 하루 대상이 약 1,850건까지
# 늘어난다(신규 500 + 1~4회차분 합). [1,2]면 정상상태가 약 1,210건(−35%)으로 줄어든다.
#
# ⚠ backfill_period의 "소스 지문이 바뀔 때만 재시도"와는 다르다 — 여기는 판매자가 링크를
# 늦게 올릴 수 있어 시간이 지나면 답이 실제로 바뀔 여지가 있다(그 여지의 크기가 위 2.4%).
# 완전히 0은 아니므로, 더 보수적으로 가려면 RESCAN_BACKOFF_DAYS=1,2,4로 4회까지 늘릴 수 있다.
BACKOFF_DAYS = [int(d) for d in os.environ.get('RESCAN_BACKOFF_DAYS', '1,2').split(',') if d.strip()]
# error는 백오프 날짜와 무관하게 매일 재시도하되(사업 신호를 기다릴 이유가 없음), 이 횟수를
# 넘으면 은퇴한다(2026-08-18 추가) — 상한이 없으면 특정 URL 패턴에서 resolve_product가 매번
# 예외를 던지는 것 같은 영구적 기술 문제도 매일 무기한 재시도 대상에 남는다. unresolved/hold의
# 기본 백오프가 약 2주를 커버하는 것과 비슷한 수준으로 잡는다.
RESCAN_ERROR_MAX_ATTEMPTS = int(os.environ.get('RESCAN_ERROR_MAX_ATTEMPTS', '14'))
STATE_FILE = ROOT / 'data/output/rescan_state.jsonl'

# stage='판단불가'인 상품도 재탐색 대상에 넣는 기간(일). 0이면 끔(예전 동작).
#
# 왜 필요한가(2026-08-19 실측) — 이 단계의 문은 gonggu_stage='진행중'인데, 그 문에 못 들어오는
# 교착이 있었다:
#     링크를 찾으려면      → rescan이 필요한데, rescan은 stage='진행중'만 본다
#     stage가 진행중이 되려면 → 기간을 찾아야 하는데
#     기간을 찾으려면      → backfill_period Tier1(몰 크롤)이 필요한데, 그건 link_status='done'만 본다
#     그런데 링크는 아직 unresolved다 ← 처음으로 돌아감
# 빠져나갈 길이 backfill Tier0(인포크 텍스트) 하나뿐이라, 그게 없거나 실패하면 아무도 다시
# 안 건드린다. 실측 규모: unresolved+hold 27518건 중 rescan이 보던 건 1643건(6%)뿐이었고,
# 판단불가에 갇힌 게 9827건이었다.
#
# 기간 제한을 두는 이유: 오래된 공구는 이미 끝나서 링크를 다시 찾아도 가치가 떨어진다. 다만
# 실측 분포상 이 풀은 의외로 신선하다 — 7일 이내 18%, 30일 이내 누적 89%, 90일 초과 0건.
# 그래서 30일이면 대부분(8745건)을 덮으면서 낡은 11%는 뺀다.
#
# ⚠ 켜는 첫 실행은 이 물량이 전부 "신규전환"으로 잡혀 한 번 크게 돈다 — LIMIT으로 며칠에
# 나눠 흘리는 걸 권한다. 그 뒤로는 백오프 스케줄이 알아서 물량을 떨어뜨린다.
RESCAN_UNKNOWN_STAGE_DAYS = int(os.environ.get('RESCAN_UNKNOWN_STAGE_DAYS', '0'))


def next_due(attempts, today):
    """attempts번째 시도를 마친 직후의 다음 예정일(ISO). 백오프를 다 썼으면 None(은퇴)."""
    if attempts - 1 < len(BACKOFF_DAYS):
        return (today + datetime.timedelta(days=BACKOFF_DAYS[attempts - 1])).isoformat()
    return None


def is_uc_owned(note):
    """이 건은 reverify_uc(uc 패스)가 소유한 물량이라 rescan이 손대면 안 되는지(2026-08-20).

    resolve가 네이버/오픈마켓 로그인월 호스트를 만나면 브라우저를 아예 안 열고 '재검증 중 차단
    — uc 패스 대상'으로 넘긴다(config.UC_LOGINWALL_HOSTS / browser.fast_skip_uc_host). 그런데
    rescan은 RESOLVE_UC를 안 켜므로 같은 fast-skip에 또 걸려 **한 글자도 다르지 않은 노트**를
    다시 쓴다 — 실측(2026-08-20): 이 노트가 후보 풀의 18.3%(638건)였고, 실제 실행에서도 처리
    670건 중 99건(14.8%)이 그대로 재생산됐다.

    ⚠ 진짜 문제는 속도가 아니라 **백오프 오염**이다. 이 no-op이 시도 횟수를 소진시켜서, uc만
    풀 수 있는 건들이 rescan에서 헛시도 5번으로 '은퇴(백오프 소진)'가 된다. 그러면 나중에 uc가
    뚫을 수 있게 돼도 rescan은 영영 안 본다. 소유권을 명확히 해서 — 이 노트가 붙은 건은
    reverify_uc가, 나머지는 rescan이 — 각자 자기 큐만 태우게 한다.

    ⚠ status='error'는 이 판정보다 먼저다(기술 실패는 uc와 무관하게 재시도할 가치가 있다)."""
    return '재검증 중 차단' in (note or '')


def classify_target(status, rec, today_iso, force=False, note=None):
    """이 상품을 이번 실행 대상에 넣을지 판단. 반환: (due 여부, 사유 라벨).

    - error는 백오프 날짜 확인 없이 포함하되, RESCAN_ERROR_MAX_ATTEMPTS번을 넘겨 계속
      error로만 끝났으면 은퇴한다(기술 실패는 사업 신호를 기다릴 이유가 없어 매일 재시도하지만,
      영원히 안 풀리는 기술 문제까지 무기한 재시도하진 않는다).
    - 이력 없음 = 진행중이 된 뒤 한 번도 재탐색 안 해봄 → 신규전환, 무조건 포함.
      (재탐색 이력은 이 스크립트가 실제로 시도했을 때만 생기므로, update_gonggu_stage가
      오늘 '진행중'으로 넘긴 새 상품은 자동으로 이 분기에 들어온다.)
    - next_due가 지났으면 백오프 도래, 남았으면 쿨다운, None이면 은퇴(보류)."""
    if status == 'error':
        if rec and rec.get('attempts', 0) >= RESCAN_ERROR_MAX_ATTEMPTS:
            return False, '은퇴(에러 반복 상한)'
        return True, '에러(무조건)'
    if is_uc_owned(note):
        return False, '제외(uc 패스 소유)'
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


def _select_sql(p, unknown_stage_days=0):
    """재탐색 후보 SELECT — 테이블/컬럼명은 platforms.py 메타에서(2단계 B4). 스케줄 필터링은
    파이썬(체크포인트)에서 하므로 SQL은 후보 풀 전체를 가져온다(SELECT 자체는 싸다 —
    비싼 건 크롤링이고, 그건 스케줄이 줄여준다).

    unknown_stage_days>0이면 stage='판단불가'인 최근 상품도 후보에 넣는다(2026-08-19,
    RESCAN_UNKNOWN_STAGE_DAYS 주석의 교착 근거 참고). 0이면 예전 동작(진행중+에러만)."""
    parent_cols = ', '.join(f'p.{c}' for c in p.parent_ctx_cols)
    # 진행중 판정을 상품(pp) 기준으로 — 기간/스테이지가 상품 단위로 이전됨(2026-08-06).
    # 예고 달력처럼 같은 포스트라도 상품마다 진행 상태가 다르므로 상품 stage로 골라야 정확하다.
    unknown_arm = ''
    if unknown_stage_days > 0:
        unknown_arm = (f"   OR (pp.gonggu_stage = '판단불가' AND pp.link_status IN ('unresolved', 'hold')\n"
                       f"       AND p.{p.date_col} >= DATE_SUB(CURDATE(), INTERVAL %s DAY))\n")
    return f"""
SELECT pp.id AS row_id, pp.product_name, pp.link_location, pp.url_type, pp.candidate_url,
       pp.sort_order, pp.link_status, pp.link_note, {parent_cols}, p.classification_note
FROM {p.product_table} pp
JOIN {p.parent_table} p ON p.{p.id_col} = pp.{p.id_col}
WHERE (pp.gonggu_stage = '진행중' AND pp.link_status IN ('unresolved', 'hold'))
   OR pp.link_status = 'error'
{unknown_arm}"""


# updated_at을 NOW()로 직접 강제 갱신하는 이유(2026-08-05): MySQL은 값이 하나도 안 바뀌면
# ON UPDATE 트리거를 안 태운다. SQL 생성은 platforms.product_update_link_sql.
UPDATE_SQL = {code: product_update_link_sql(p) for code, p in PLATFORMS.items()}


def _fetch_candidates(conn, unknown_stage_days=None):
    days = RESCAN_UNKNOWN_STAGE_DAYS if unknown_stage_days is None else unknown_stage_days
    out = []
    with conn.cursor() as cur:
        for code, p in PLATFORMS.items():
            sql = _select_sql(p, days)
            cur.execute(sql, (days,) if days > 0 else ())
            for r in cur.fetchall():
                parent = parent_ctx_from_row(p, r)
                product = {'product_name': r['product_name'], 'link_location': r['link_location'],
                           'url_type': r['url_type'], 'candidate_url': r['candidate_url'],
                           'sort_order': r['sort_order']}
                out.append((code, parent, product, r['row_id'], r['link_status'], r['link_note']))
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
    for code, parent, product, row_id, link_status, link_note in candidates:
        key = product_key(code, parent, product['sort_order'])
        due, reason = classify_target(link_status, state.get(key), today_iso,
                                      force=RESCAN_FORCE, note=link_note)
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

    # resolve와 같은 진단을 여기서도 찍는다(2026-08-20). 예전엔 패스트패스 적중률 한 줄뿐이라
    # "브라우저를 실제로 몇 %나 썼는지", "허가증이 놀았는지 모자랐는지"를 알 수 없었고, 그래서
    # 이 단계 튜닝이 계속 추정으로 갔다 — resolve에서는 같은 진단 덕에 "유휴 76%인데 대기는
    # 135초뿐 → 구조 개편 불필요"를 숫자로 판정할 수 있었다(2026-08-20). 같은 눈을 여기에도 단다.
    _print_resolution_diagnostics(httpfetch_stats(), via_stats(), body_too_short_samples())
    by_status = {k: v for k, v in counters.items() if not k.startswith('_')}
    print(f'재탐색 완료 {len(targets)}건 — {by_status}')


if __name__ == '__main__':
    main()
