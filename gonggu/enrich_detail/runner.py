"""enrich_detail 오케스트레이션 — 대상 선정 → 크롤링 → 코드 추출 → LLM → 검증 → DB.

워커 풀 배관은 crawl_pool(LazyPage/MAX_BROWSERS 안전판), 안티봇 대기는 resolve_links의
ITEM_DELAY(_SMART) 값을 그대로 공유한다. 진행 로그/카운터 스타일은 rescan_inprogress와
동일하게 맞춘다(운영자가 같은 눈으로 읽을 수 있게).

사용법:
    python3 -m gonggu.enrich_detail
    LIMIT=10 python3 -m gonggu.enrich_detail
    PLATFORM=ig DETAIL_CONCURRENCY=8 python3 -m gonggu.enrich_detail
"""
import os
import sys

from gonggu.common import DEEPSEEK_KEY, acquire_lock, connect_dst
from gonggu.crawl_pool import run_crawl_pool
from gonggu.resolve_links.config import ITEM_DELAY, ITEM_DELAY_SMART, MAX_BROWSERS
from gonggu.resolve_links.urlutil import host_of

from .config import DETAIL_CONCURRENCY, MAX_ERROR_LEN, PAGE_TEXT_LIMIT
from .extract import extract_facts
from .fetchpage import fetch_detail_page
from .images import build_image_rows
from .llm import call_category, call_detail_enrich
from .targets import fetch_captions, fetch_targets
from .validate import merge_and_validate
from .writeback import write_done, write_status


def process_target(page, code, row, caption):
    """상품 1건: 크롤링→추출→LLM→검증. 반환: (status, fields|None, image_rows, error|None, 진단문자열).
    DB는 건드리지 않는다(호출부가 lock 안에서 반영) — 테스트에서 이 함수만 따로 돌릴 수 있게.
    진단문자열(경로/추출소스/도메인)은 콘솔 로그용 — "왜 이 필드가 NULL이지?"를 로그만으로
    절반은 답할 수 있게 한다(실전 스모크 검수에서 필요성 확인, 2026-08-06)."""
    host = host_of(row['candidate_url'] or '')
    rec = fetch_detail_page(page, row['candidate_url'])
    dbg = f"{rec.get('via', '?')}·{host}"
    if rec['gone']:
        return 'gone', None, [], f"페이지 소멸({host}): {rec['gone']}", dbg
    if rec['error'] or not rec['html']:
        return 'error', None, [], f"크롤링 실패({host}): {rec['error'] or '빈 응답'}", dbg

    facts = extract_facts(rec['html'], rec['final_url'] or row['candidate_url'], PAGE_TEXT_LIMIT)
    dbg = f"{rec.get('via', '?')}·{facts.get('source') or '추출없음'}·{host}"

    llm_out, llm_err = call_detail_enrich(
        product_name=row['product_name'], caption=caption, facts=facts,
        gonggu_stage=row.get('gonggu_stage'), publish_date=row.get('publish_date'))
    if llm_out is None:
        return 'error', None, [], f'LLM#5 실패: {llm_err}', dbg

    category, subcategory = call_category(
        product_name=facts.get('product_name') or row['product_name'],
        title=row.get('parent_title') or '', caption=caption)

    fields = merge_and_validate(llm_out, facts, caption, category, subcategory)
    image_rows = build_image_rows(facts['thumbnail_urls'], facts['detail_image_urls'])
    return 'done', fields, image_rows, None, dbg


def main():
    acquire_lock('enrich_detail')
    if not DEEPSEEK_KEY:
        print('.env에 DEEPSEEK_KEY가 필요합니다.', file=sys.stderr)
        sys.exit(1)

    only_platform = os.environ.get('PLATFORM') or None
    conn = connect_dst()
    try:
        targets = fetch_targets(conn, only_platform)
    finally:
        conn.close()

    # 호스트 스킵 — 2단 백필 전략의 1단(무인 대량)에서 uc가 필요한 호스트(네이버·오픈마켓)를
    # 통째로 빼고 자사몰만 빠르게 처리할 때 쓴다. DETAIL_SKIP_HOSTS=naver.,gmarket.co.kr,...
    # 스킵된 건은 detail 행이 안 생기므로 2단(uc)에서 자동으로 다시 대상에 잡힌다.
    # DETAIL_SKIP_NAVER=1은 하위호환(=DETAIL_SKIP_HOSTS에 naver. 추가).
    skip = [h for h in os.environ.get('DETAIL_SKIP_HOSTS', '').split(',') if h]
    if os.environ.get('DETAIL_SKIP_NAVER', '0') == '1' and 'naver.' not in skip:
        skip.append('naver.')
    if skip:
        before = len(targets)
        targets = [(c, r) for c, r in targets
                   if not any(k in host_of(r['candidate_url'] or '') for k in skip)]
        print(f'  스킵 호스트 {skip} — {before - len(targets)}건 제외(2단 uc에서 자동 재대상)')

    total = len(targets)
    limit = int(os.environ.get('LIMIT', '0')) or total
    targets = targets[:limit]
    retry_n = sum(1 for _, r in targets if r.get('prev_status') == 'error')
    print(f"상세 수집 대상 {total}건 (link_status=done & detail 미완) → 이번 실행 {len(targets)}건"
          f"{f' (LIMIT으로 {total - len(targets)}건 보류)' if len(targets) < total else ''}"
          f"{f' — 그중 error 재시도 {retry_n}건' if retry_n else ''}")
    if not targets:
        print('  오늘은 상세 수집할 것이 없습니다.')
        return

    captions = fetch_captions(targets)
    print(f'  원본 캡션 확보 {len(captions)}건 / 대상 {len(targets)}건 '
          f'(없는 건 크롤링 결과만으로 진행)')
    print(f'  — 동시 워커 상한 {DETAIL_CONCURRENCY}개, 브라우저 상한 {MAX_BROWSERS}개')

    counters = {}

    def handle(ctx, target):
        code, row = target
        db = ctx.state  # 워커당 DB 커넥션 1개(pymysql 커넥션은 스레드 간 공유 금지)
        caption = captions.get((code, row['native_id']), '')
        try:
            status, fields, image_rows, err, dbg = process_target(ctx.page, code, row, caption)
        except Exception as e:  # 예상 밖 예외 — 이 상품만 error로 남기고 워커는 계속
            status, fields, image_rows, err, dbg = ('error', None, [],
                                                    f'예외: {str(e)[:MAX_ERROR_LEN - 10]}', '')

        with ctx.lock:
            try:
                if status == 'done':
                    write_done(db, code, row['product_row_id'], fields, image_rows)
                else:
                    write_status(db, code, row['product_row_id'], status, err)
            except Exception as e:
                status, err = 'error', f'DB 저장 실패: {str(e)[:120]}'
            counters[status] = counters.get(status, 0) + 1
            counters['_n'] = counters.get('_n', 0) + 1
            key = f"{code}:{row['native_id']}#{row['product_row_id']}"
            shown = err or (f"가격 {fields.get('sale_price')}원, 이미지 {len(image_rows)}장"
                            if fields else '')
            print(f"  [{counters['_n']}/{len(targets)}] (w{ctx.worker_id}) {key} -> {status} "
                  f"[{dbg}] {str(shown)[:70]}", flush=True)

    run_crawl_pool(targets, handle, concurrency=DETAIL_CONCURRENCY, item_delay=ITEM_DELAY,
                   delay_only_after_browser=ITEM_DELAY_SMART,
                   worker_setup=connect_dst, worker_teardown=lambda db: db.close(),
                   warn_hint='DETAIL_CONCURRENCY')

    by_status = {k: v for k, v in counters.items() if not k.startswith('_')}
    print(f'상세 수집 완료 {len(targets)}건 — {by_status}')


if __name__ == '__main__':
    main()
