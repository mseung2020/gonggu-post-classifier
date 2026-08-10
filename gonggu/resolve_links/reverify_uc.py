#!/usr/bin/env python3
"""2단 재검증(uc) — resolve가 "재검증 중 차단(로그인월/캡차)"으로 포기했던 상품을 골라,
undetected_chromedriver(uc) 엔진으로 그 페이지를 실제로 열어 재검증을 다시 시도한다.

배경: bare 링크인바이오 버튼(source='link')은 과거 오탐(스토어 메인이 엉뚱한 상품으로 확정된
2026-07-21 사고) 때문에 확신도가 high여도 반드시 실제 페이지를 열어 LLM#3로 검증한다
(picker.finalize_pick의 force_verify). 그런데 네이버 스마트스토어가 로그인월/캡차로 막으면
"내용 못 본 채 확정 안 함"으로 unresolved가 됐다 — 링크가 틀려서가 아니라 검증을 못 해서 버린
것. enrich_detail에서 네이버를 뚫는 그 uc 엔진(gonggu.uc_engine)을 resolve의 재검증 크롤링에도
붙여(RESOLVE_UC=1) 진짜로 열어보고 판정한다. 차단을 *우회*해 무조건 통과시키는 게 아니라, 막혀서
못 하던 검증을 *작동*시키는 것 — 오판 방지 안전장치는 그대로 살아있다(uc로 연 페이지도 LLM#3
재검증을 통과해야 done).

대상: link_status='unresolved' 이고 link_note에 '재검증 중 차단'이 있는 상품(2026-08-07 추가한
link_note 컬럼이 그대로 작업 큐가 된다). gonggu_stage는 안 따진다 — 공구가 끝났어도 링크 자산은
enrich에 쓰이므로 확정할 가치가 있다.

⚠ 이 패스만 uc를 켠다(모듈 상단에서 RESOLVE_UC=1 자동 설정) — 본 resolve/대량 경로는 그대로.
uc는 실제 크롬 창 + (필요 시) 사람이 캡차 통과라, enrich stage 2처럼 사람이 곁에 있을 때 낮은
동시성으로 돌린다. 먼저 전용 프로필에 네이버 신뢰를 쌓아둘 것:
    python3 -m gonggu.enrich_detail.warmup_naver_uc

DB 상태가 곧 체크포인트다(idempotent) — done이 되면 대상에서 빠지고, uc가 못 뚫은 건 여전히
unresolved(note 갱신)로 남아 다음 실행에 다시 잡힌다.

사용법(저장소 루트에서):
    python3 -m gonggu.resolve_links.reverify_uc
    LIMIT=20 python3 -m gonggu.resolve_links.reverify_uc
    REVERIFY_CONCURRENCY=1 python3 -m gonggu.resolve_links.reverify_uc
    RESOLVE_UC_HOSTS=naver.,gmarket.co.kr python3 -m gonggu.resolve_links.reverify_uc  # 오픈마켓도
"""
import os

# uc 폴백을 이 패스 한정으로 강제한다 — browser.fetch가 차단 시 uc로 재시도하도록. 다른
# import보다 먼저 세팅해야(browser._uc_enabled_for는 호출 시점에 읽지만, 명시적으로 앞에 둔다).
os.environ.setdefault('RESOLVE_UC', '1')

import sys  # noqa: E402

from gonggu.common import DEEPSEEK_KEY, connect_dst  # noqa: E402
from gonggu.crawl_pool import run_crawl_pool  # noqa: E402
from gonggu.platforms import PLATFORMS, parent_ctx_from_row, product_update_link_sql  # noqa: E402
from gonggu.resolve_links.config import ITEM_DELAY, ITEM_DELAY_SMART, MAX_BROWSERS  # noqa: E402
from gonggu.resolve_links.core import resolve_product  # noqa: E402

CONCURRENCY = int(os.environ.get('REVERIFY_CONCURRENCY', '2'))
BLOCKED_NOTE_LIKE = '%재검증 중 차단%'
UPDATE_SQL = {code: product_update_link_sql(p) for code, p in PLATFORMS.items()}


def _select_sql(p):
    """차단으로 포기한 unresolved 상품. 컬럼 구성은 rescan_inprogress와 동일(parent 재구성 +
    resolve_product 입력). WHERE만 '차단 note'로 좁힌다 — note는 파라미터로 넘겨 % 이스케이프 안전."""
    parent_cols = ', '.join(f'p.{c}' for c in p.parent_ctx_cols)
    return f"""
SELECT pp.id AS row_id, pp.product_name, pp.link_location, pp.url_type, pp.candidate_url,
       pp.sort_order, pp.link_status, {parent_cols}, p.classification_note
FROM {p.product_table} pp
JOIN {p.parent_table} p ON p.{p.id_col} = pp.{p.id_col}
WHERE pp.link_status = 'unresolved' AND pp.link_note LIKE %s
"""


def _fetch_targets(conn):
    out = []
    with conn.cursor() as cur:
        for code, p in PLATFORMS.items():
            cur.execute(_select_sql(p), (BLOCKED_NOTE_LIKE,))
            for r in cur.fetchall():
                parent = parent_ctx_from_row(p, r)
                product = {'product_name': r['product_name'], 'link_location': r['link_location'],
                           'url_type': r['url_type'], 'candidate_url': r['candidate_url'],
                           'sort_order': r['sort_order']}
                out.append((code, parent, product, r['row_id']))
    return out


def main():
    if not DEEPSEEK_KEY:
        print('.env에 DEEPSEEK_KEY가 필요합니다.', file=sys.stderr)
        sys.exit(1)

    conn = connect_dst()
    try:
        targets = _fetch_targets(conn)
    finally:
        conn.close()

    total = len(targets)
    limit = int(os.environ.get('LIMIT', '0')) or total
    targets = targets[:limit]
    print(f"차단으로 포기했던 unresolved {total}건 → 이번 실행 {len(targets)}건"
          f"{f' (LIMIT으로 {total - len(targets)}건 보류)' if len(targets) < total else ''}")
    print(f"  uc 엔진 ON(RESOLVE_UC={os.environ.get('RESOLVE_UC')}, "
          f"대상 호스트 {os.environ.get('RESOLVE_UC_HOSTS', 'naver.')}), 동시 {CONCURRENCY}, "
          f"브라우저 상한 {MAX_BROWSERS}")
    if not targets:
        print('  재검증할 차단 건이 없습니다.')
        return

    counters = {}

    def handle(ctx, target):
        code, parent, product, row_id = target
        db = ctx.state  # 워커당 DB 커넥션 1개
        try:
            res = resolve_product(ctx.page, code, parent, product)
        except Exception as e:
            res = {'status': 'error', 'final_url': None, 'note': f'예외: {str(e)[:150]}'}
        try:
            candidate_url = res.get('candidate_url') or product['candidate_url']
            new_candidate_url = candidate_url[:500] if candidate_url else None
            with ctx.lock:
                db.ping(reconnect=True)
                with db.cursor() as cur:
                    note = res.get('note')
                    cur.execute(UPDATE_SQL[code],
                                (new_candidate_url, res['status'], note[:255] if note else None, row_id))
                db.commit()
                counters[res['status']] = counters.get(res['status'], 0) + 1
                counters['_n'] = counters.get('_n', 0) + 1
                shown = res.get('final_url') or res.get('note', '')
                print(f"  [{counters['_n']}/{len(targets)}] (w{ctx.worker_id}) row {row_id} -> "
                      f"{res['status']} {str(shown)[:70]}", flush=True)
        except Exception as e:
            with ctx.lock:
                counters['error'] = counters.get('error', 0) + 1
                counters['_n'] = counters.get('_n', 0) + 1
                print(f"  [{counters['_n']}/{len(targets)}] (w{ctx.worker_id}) row {row_id} -> "
                      f"저장 실패(스킵): {str(e)[:120]}", flush=True)

    run_crawl_pool(targets, handle, concurrency=CONCURRENCY, item_delay=ITEM_DELAY,
                   delay_only_after_browser=ITEM_DELAY_SMART,
                   worker_setup=connect_dst, worker_teardown=lambda db: db.close(),
                   warn_hint='REVERIFY_CONCURRENCY')

    by = {k: v for k, v in counters.items() if not k.startswith('_')}
    print(f'2단 재검증 완료 {len(targets)}건 — {by} (done 승격 {counters.get("done", 0)}건)')


if __name__ == '__main__':
    main()
