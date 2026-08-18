"""1단계(분리 실행) — 크롤링+추출만. LLM은 절대 안 부른다.

runner.py의 단일실행 경로(크롤링→LLM→DB를 상품 1건마다 순서대로)와 달리, 이 모듈은
크롤링/추출까지만 하고 결과를 jsonl 체크포인트에 쌓아둔다. LLM을 기다리지 않으므로
브라우저(특히 uc — 드라이버 1개뿐)가 쉬지 않고 계속 다음 상품을 문다. 나머지(LLM→DB)는
llm_stage.py/load_stage.py가 이어서 처리한다(대공사, 2026-08-13).

크롤링 실패(gone/blocked/error)는 지금처럼 그 즉시 DB에 상태만 기록한다(write_status) —
jsonl에는 성공(크롤링 완료)한 건만 남긴다.

사용법:
    python3 -m gonggu.enrich_detail.crawl_stage                  # fast 모드 기본
    DETAIL_MODE=uc python3 -m gonggu.enrich_detail.crawl_stage   # uc 모드
    LIMIT=10 python3 -m gonggu.enrich_detail.crawl_stage
    SHARD_COUNT=5 SHARD_INDEX=0 UC_PROFILE=... python3 -m gonggu.enrich_detail.crawl_stage
        (runner.py와 동일한 SHARD_COUNT/SHARD_INDEX/DETAIL_SKIP_HOSTS 지원 — 출력 파일도
         샤드별로 나뉜다: detail_crawled_shard{N}.jsonl)
"""
import os
import sys

from gonggu.common import DEEPSEEK_KEY, ROOT, acquire_lock, append_jsonl, connect_dst, load_jsonl
from gonggu.crawl_pool import run_crawl_pool
from gonggu.resolve_links.config import ITEM_DELAY, ITEM_DELAY_SMART, MAX_BROWSERS
from gonggu.resolve_links.urlutil import host_of

from .config import DETAIL_CONCURRENCY, DETAIL_MODE, MAX_ERROR_LEN
from .runner import crawl_one
from .targets import fetch_captions, fetch_targets
from .writeback import write_status

OUTPUT_DIR = ROOT / 'data/output'


def _output_path(shard_count, shard_index):
    if shard_count > 1:
        return OUTPUT_DIR / f'detail_crawled_shard{shard_index}.jsonl'
    return OUTPUT_DIR / 'detail_crawled.jsonl'


def main():
    shard_count = int(os.environ.get('SHARD_COUNT', '1'))
    shard_index = int(os.environ.get('SHARD_INDEX', '0'))
    lock_name = f'enrich_detail_crawl_shard{shard_index}' if shard_count > 1 else 'enrich_detail_crawl'
    acquire_lock(lock_name)
    if not DEEPSEEK_KEY:
        print('.env에 DEEPSEEK_KEY가 필요합니다.', file=sys.stderr)
        sys.exit(1)

    out_path = _output_path(shard_count, shard_index)
    already_crawled = set(load_jsonl(out_path).keys())  # 재실행 시 이미 크롤링해둔 건 다시 안 금

    only_platform = os.environ.get('PLATFORM') or None
    conn = connect_dst()
    try:
        targets = fetch_targets(conn, only_platform, DETAIL_MODE)
    finally:
        conn.close()

    skip = [h for h in os.environ.get('DETAIL_SKIP_HOSTS', '').split(',') if h]
    if os.environ.get('DETAIL_SKIP_NAVER', '0') == '1' and 'naver.' not in skip:
        skip.append('naver.')
    if skip:
        before = len(targets)
        targets = [(c, r) for c, r in targets
                   if not any(k in host_of(r['candidate_url'] or '') for k in skip)]
        print(f'  스킵 호스트 {skip} — {before - len(targets)}건 제외')

    if shard_count > 1:
        before = len(targets)
        targets = [(c, r) for c, r in targets if r['product_row_id'] % shard_count == shard_index]
        print(f'  샤딩 {shard_index}/{shard_count} — 전체 {before}건 중 이 프로세스 담당 {len(targets)}건')

    before = len(targets)
    targets = [(c, r) for c, r in targets
               if f"{c}:{r['native_id']}#{r['product_row_id']}" not in already_crawled]
    if before - len(targets):
        print(f'  이미 크롤링 완료(체크포인트에 있음) {before - len(targets)}건 제외')

    total = len(targets)
    limit = int(os.environ.get('LIMIT', '0')) or total
    targets = targets[:limit]
    print(f'[{DETAIL_MODE} 모드/crawl_stage] 크롤링 대상 {total}건 → 이번 실행 {len(targets)}건'
          f"{f' (LIMIT으로 {total - len(targets)}건 보류)' if len(targets) < total else ''}")
    if not targets:
        print('  오늘은 크롤링할 것이 없습니다.')
        return

    captions = fetch_captions(targets)
    print(f'  원본 캡션 확보 {len(captions)}건 / 대상 {len(targets)}건, → {out_path.name}')
    print(f'  — 동시 워커 상한 {DETAIL_CONCURRENCY}개, 브라우저 상한 {MAX_BROWSERS}개')

    counters = {}

    def handle(ctx, target):
        code, row = target
        db = ctx.state
        caption = captions.get((code, row['native_id']), '')
        try:
            status, facts, err, dbg = crawl_one(ctx.page, row)
        except Exception as e:  # 예상 밖 예외 — 이 상품만 error로 남기고 워커는 계속
            status, facts, err, dbg = ('error', None, f'예외: {str(e)[:MAX_ERROR_LEN - 10]}', '')

        # uc 패스에서 crawled가 아닌 결과(error/blocked)는 전부 blocked로 남긴다 — runner.py의
        # 기존 정책과 동일(uc 큐 안에 머물러 다음 uc 실행에서 다시 시도).
        if DETAIL_MODE == 'uc' and status == 'error':
            status = 'blocked'

        key = f"{code}:{row['native_id']}#{row['product_row_id']}"
        with ctx.lock:
            if status == 'crawled':
                append_jsonl(out_path, {
                    'key': key, 'code': code, 'product_row_id': row['product_row_id'],
                    'product_name': row['product_name'], 'parent_title': row.get('parent_title'),
                    'gonggu_stage': row.get('gonggu_stage'), 'publish_date': row.get('publish_date'),
                    'caption': caption, 'facts': facts,
                })
            else:
                try:
                    write_status(db, code, row['product_row_id'], status, err)
                except Exception as e:
                    status, err = 'error', f'DB 저장 실패: {str(e)[:120]}'
            counters[status] = counters.get(status, 0) + 1
            counters['_n'] = counters.get('_n', 0) + 1
            print(f"  [{counters['_n']}/{len(targets)}] (w{ctx.worker_id}) {key} -> {status} "
                  f"[{dbg}] {str(err or '')[:70]}", flush=True)

    run_crawl_pool(targets, handle, concurrency=DETAIL_CONCURRENCY, item_delay=ITEM_DELAY,
                   delay_only_after_browser=ITEM_DELAY_SMART,
                   worker_setup=connect_dst, worker_teardown=lambda db: db.close(),
                   warn_hint='DETAIL_CONCURRENCY')

    by_status = {k: v for k, v in counters.items() if not k.startswith('_')}
    print(f'크롤링 완료 {len(targets)}건 — {by_status} → {out_path}')


if __name__ == '__main__':
    main()
