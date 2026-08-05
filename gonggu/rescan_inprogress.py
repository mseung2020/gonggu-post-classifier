#!/usr/bin/env python3
"""5단계 보강: gonggu_stage='진행중'인데 link_status가 'unresolved'/'hold'인 상품만 골라 링크
해석을 다시 시도한다. 시작전 단계에서는 인포크 등에 아직 구매 링크가 없어서 못 찾았을 뿐인데,
진행중이 되면 실제로 링크가 채워지는 경우가 많아서(원준님 피드백 반영) 이 전이 시점을
노려 재탐색한다. hold도 unresolved와 동일하게 취급한다(2026-07-29 결정) — hold도 결국
candidate_url이 허브 URL 하나로 남아있는 경우가 많아서 재확인할 가치가 같다.

link_status='error'는 위 두 상태와 성격이 달라 조건을 따로 둔다(2026-08-04 추가) — unresolved/
hold는 "아직 링크가 없어서" 못 찾은 것이라 진행중 전환이라는 사업적 신호를 기다릴 이유가
있지만, error는 크롤링/LLM 호출 자체가 실패한 순수 기술적 문제(예: DeepSeek 타임아웃 폭주)라
gonggu_stage와 무관하게 재시도할 가치가 있다. 그래서 error는 진행중 여부를 안 따지고 무조건
대상에 포함한다.

resolve_links의 실제 판단/크롤링 로직(resolve_product)과 안티봇 대응(도메인당 동시 접근
제한 — browser.fetch()/redirect.follow_redirect() 내부의 domain_gate)을 그대로 재사용하고,
워커 풀 배관은 crawl_pool.py 공용 모듈(2단계 B3), 플랫폼별 SQL은 platforms.py
메타테이블(2단계 B4)을 쓴다. load_ready/link_resolution 파일을 거치지 않고 DB에서 직접
대상을 뽑아 DB에 직접 반영한다. candidate_url은 성공/실패와 무관하게 항상 대표 URL 1개다
(resolve_product가 반환하는 candidate_url 필드 참고) — 원본 다중 후보를 DB에 보존해뒀다가
다시 꺼내 쓰는 방식이 아니라, 매번 그 시점의 candidate_url(대부분 링크인바이오 허브 URL)
하나를 다시 열어봐서 "그 사이에 새 링크가 붙었는지"만 확인하는 것이 이 재탐색의 목적이다.

link_resolution.jsonl에도 같은 키로 결과를 append해서, 정기 파이프라인이 나중에
04_resolved를 다시 조립할 때 이 재탐색 결과가 잊히지 않게 한다(파일과 DB가 항상 같은
진실을 가리키게 유지). 여전히 unresolved/hold면 그대로 둔다.

사용법:
    python3 -m gonggu.rescan_inprogress            # 전체 대상
    LIMIT=50 python3 -m gonggu.rescan_inprogress   # 앞에서 50건만(테스트용)
    RESCAN_CONCURRENCY=40 python3 -m gonggu.rescan_inprogress
    RESCAN_SKIP_TODAY=0 python3 -m gonggu.rescan_inprogress   # 하루 제한 무시하고 강제로 전체 재시도

RESCAN_CONCURRENCY는 "동시에 처리 중인 상품 수"지 "동시에 뜨는 크롬 수"가 아니다 — 실제
브라우저 개수는 MAX_BROWSERS가 따로 제한한다(crawl_pool/browser.py 참고).

RESCAN_SKIP_TODAY(기본 1) — 오늘 날짜 안에 이미 한 번 손댄(updated_at) 행은 대상에서 뺀다
(2026-08-04 추가). 자정이 지나면 날짜가 바뀌어 자동으로 초기화된다 — 분 단위 쿨다운이 아니라
"하루에 한 번"이라 계산이 단순하고, 오늘 도중에 이 스크립트를 몇 번을 중간에 멈추고 다시
돌려도(예: DeepSeek 503 폭주 중 재시작 반복) 오늘 이미 시도했던 건은 다시 안 건드린다. 별도
체크포인트 파일 없이 이미 있는 updated_at 컬럼(ON UPDATE CURRENT_TIMESTAMP)만 쓴다. 오늘 안에
꼭 다시 시도해야 하면 RESCAN_SKIP_TODAY=0으로 끄고 돌릴 것.
"""
import datetime
import os
import sys

from gonggu.common import DEEPSEEK_KEY, append_jsonl, connect_dst
from gonggu.crawl_pool import run_crawl_pool
from gonggu.platforms import PLATFORMS, parent_ctx_from_row, product_update_link_sql
from gonggu.resolve_links.config import HTTP_FAST_PATH, ITEM_DELAY, MAX_BROWSERS, RESOLUTION_FILE
from gonggu.resolve_links.core import resolve_product
from gonggu.resolve_links.httpfetch import stats as httpfetch_stats
from gonggu.resolve_links.matching import product_key

RESCAN_CONCURRENCY = int(os.environ.get('RESCAN_CONCURRENCY', '4'))
RESCAN_SKIP_TODAY = os.environ.get('RESCAN_SKIP_TODAY', '1') != '0'


def _cutoff_ts():
    """RESCAN_SKIP_TODAY가 켜져 있으면 '오늘 00:00'을 돌려준다 — updated_at이 이보다 이르면
    (=오늘 안 건드림) 대상에 포함(조건은 `updated_at < cutoff_ts`). 꺼져 있으면 사실상 모든
    행이 조건을 통과하도록 아주 먼 미래 시각을 돌려준다(과거를 주면 정반대로 거의 아무것도
    안 걸림 — 2026-08-04에 실측으로 잡은 버그). MySQL의 CURDATE()/NOW() 대신 파이썬의 오늘
    날짜를 기준으로 계산해서 나머지 파이프라인(transform.py의 _compute_stage 등)과 같은
    "오늘"의 기준을 쓴다."""
    if not RESCAN_SKIP_TODAY:
        return datetime.datetime(9999, 12, 31)
    today = datetime.date.today()
    return datetime.datetime(today.year, today.month, today.day)


def _select_sql(p):
    """재탐색 대상 SELECT — 테이블/컬럼명은 platforms.py 메타에서(2단계 B4), WHERE 조건은
    이 스크립트 고유(진행중+unresolved/hold, 또는 error 무조건 / 오늘 안 건드린 것만)."""
    parent_cols = ', '.join(f'p.{c}' for c in p.parent_ctx_cols)
    return f"""
SELECT pp.id AS row_id, pp.product_name, pp.link_location, pp.url_type, pp.candidate_url,
       pp.sort_order, {parent_cols}, p.classification_note
FROM {p.product_table} pp
JOIN {p.parent_table} p ON p.{p.id_col} = pp.{p.id_col}
WHERE ((p.gonggu_stage = '진행중' AND pp.link_status IN ('unresolved', 'hold'))
   OR pp.link_status = 'error')
  AND pp.updated_at < %s
"""


# updated_at을 ON UPDATE CURRENT_TIMESTAMP 자동 트리거에만 맡기지 않고 NOW()로 직접 강제
# 갱신한다(2026-08-05 추가) — MySQL은 UPDATE 문이 실행돼도 값이 실제로 하나도 안 바뀌면
# 자동 트리거를 안 태운다. "크롤링할 후보 링크 없음"처럼 이번에도 똑같이 candidate_url=NULL,
# link_status='unresolved'로 끝나는 항목은 값이 그대로라 updated_at이 안 갱신되고, 그러면
# RESCAN_SKIP_TODAY가 "오늘 안 건드림"으로 착각해서 매번 다시 대상에 잡힌다(실측 확인 —
# 실패가 반복되는 항목일수록 이 버그에 잘 걸림). SQL 생성은 platforms.product_update_link_sql.
UPDATE_SQL = {code: product_update_link_sql(p) for code, p in PLATFORMS.items()}


def _fetch_targets(conn, cutoff_ts):
    targets = []
    with conn.cursor() as cur:
        for code, p in PLATFORMS.items():
            cur.execute(_select_sql(p), (cutoff_ts,))
            for r in cur.fetchall():
                parent = parent_ctx_from_row(p, r)
                product = {'product_name': r['product_name'], 'link_location': r['link_location'],
                           'url_type': r['url_type'], 'candidate_url': r['candidate_url'],
                           'sort_order': r['sort_order']}
                targets.append((code, parent, product, r['row_id']))
    return targets


def main():
    if not DEEPSEEK_KEY:
        print('.env에 DEEPSEEK_KEY가 필요합니다.', file=sys.stderr)
        sys.exit(1)

    conn = connect_dst()
    try:
        targets = _fetch_targets(conn, _cutoff_ts())
    finally:
        conn.close()

    limit = int(os.environ.get('LIMIT', '0')) or len(targets)
    targets = targets[:limit]
    skip_note = '오늘 이미 시도한 건 제외' if RESCAN_SKIP_TODAY else '하루 제한 없이 전체'
    if not targets:
        print(f'진행중+unresolved/hold 또는 error 재탐색 대상 0건 ({skip_note})')
        return

    counters = {}
    print(f'진행중+unresolved/hold 또는 error 재탐색 대상 {len(targets)}건({skip_note}) '
          f'— 동시 워커 상한 {RESCAN_CONCURRENCY}개, 브라우저 상한 {MAX_BROWSERS}개, '
          f'requests 패스트패스 {"ON" if HTTP_FAST_PATH else "OFF"}')

    def handle(ctx, target):
        platform, parent, product, row_id = target
        db = ctx.state  # 워커당 DB 커넥션 1개(pymysql 커넥션은 스레드 간 공유가 안전하지 않음)
        try:
            res = resolve_product(ctx.page, platform, parent, product)
        except Exception as e:
            res = {'status': 'error', 'final_url': None, 'note': str(e)[:160]}

        # 이 블록에서 뭐가 터지든(DB 접속 끊김, 예상 못 한 None 등) 이 상품 처리만 실패로
        # 남기고 워커는 죽지 않게 한다(crawl_pool의 마지막 방어선과 별개로, 여기서 잡아야
        # "저장 실패"를 카운터에 정확히 반영할 수 있다 — 2026-08-04 실측 사연은 위 docstring).
        try:
            key = product_key(platform, parent, product['sort_order'])
            # candidate_url은 상태와 무관하게 항상 단일 URL이길 기대하지만(2026-07-29 결정),
            # 애초에 후보 자체가 없어서 res도 product도 둘 다 None인 경우가 있을 수 있다 —
            # 그럴 땐 값을 지어내지 말고 그대로 NULL로 둔다.
            candidate_url = res.get('candidate_url') or product['candidate_url']
            new_candidate_url = candidate_url[:500] if candidate_url else None

            with ctx.lock:
                # 크롤링/LLM 처리(수 초~수십 초)가 끝난 뒤에야 이 커넥션을 다시 쓰는 구조라,
                # 그 사이 방화벽/DB의 idle 타임아웃으로 끊겨 있을 수 있다(실측, 2026-08-04 —
                # "Lost connection to MySQL server during query" 대량 발생). ping으로 끊겼으면
                # 자동 재연결부터 하고 실행한다.
                db.ping(reconnect=True)
                with db.cursor() as cur:
                    cur.execute(UPDATE_SQL[platform], (new_candidate_url, res['status'], row_id))
                db.commit()
                append_jsonl(RESOLUTION_FILE, {**res, 'key': key})
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
