"""체크포인트/CLI 진입점. 실제 판단 로직은 core.py에 있고, 워커 풀 배관은 crawl_pool.py
공용 모듈(2단계 B3)을 쓴다 — 여기는 "무엇을 해석 대상으로 삼고 결과를 어디에 남기는지"만 담당.

워커/브라우저 수명에 대한 실측 기반 설계(워커≠브라우저, MAX_BROWSERS 허가증,
release_if_contended)는 crawl_pool.py와 browser.py의 docstring 참고.

도메인당 동시 접근 상한(MAX_PER_DOMAIN)은 여기서 스케줄링 단위로 걸지 않는다 — 상품의
"첫 후보 URL" 도메인(예: 링크인바이오 허브)을 기준으로 걸면 실제 무거운 Playwright 접근이
일어나는 곳(LLM#2가 고른 최종 목적지, 전혀 다른 도메인일 수 있음)을 못 보호하면서 정작
가벼운 단계(캐시된 requests 호출)만 묶어두는 문제가 있었다(실측 확인, 2026-07-27). 대신
browser.fetch()/redirect.follow_redirect() 안에서 "실제로 page.goto()를 여는 그 순간" 목적지
도메인 기준으로 게이팅한다(domain_gate 참고)."""
import sys

from gonggu.common import (DEEPSEEK_KEY, LOAD_READY_DIR, RESOLVED_DIR, append_jsonl, clear_json_dir,
                     dump_jsonl_sharded, load_json_dir, load_jsonl, parent_date_key)
from gonggu.crawl_pool import run_crawl_pool

from .config import HTTP_FAST_PATH, ITEM_DELAY, MAX_BROWSERS, RESOLUTION_FILE, RESOLVE_CONCURRENCY
from .httpfetch import stats as httpfetch_stats
from .core import resolve_product
from .matching import product_key


def load_resolutions():
    """key당 res를 담은 dict로 복원 — resolutions[key]에는 'key' 필드가 없어야 기존
    build_resolved_file 등 소비 코드가 그대로 동작하므로 로드 시 벗겨낸다."""
    raw = load_jsonl(RESOLUTION_FILE)
    return {k: {kk: vv for kk, vv in rec.items() if kk != 'key'} for k, rec in raw.items()}


def build_resolved_file(items, resolutions):
    out = []
    for item in items:
        platform, parent = item['platform'], item['parent']
        new_products = []
        for p in item['products']:
            key = product_key(platform, parent, p['sort_order'])
            res = resolutions.get(key)
            np = dict(p)
            # link_status = 이 candidate_url이 검증된 최종 상품페이지(done)인지, 아니면 아직
            # 확인 못 한 중간 단계(unresolved/hold/error)인지 — 개발자가 "바로 스크래핑 가능한지
            # vs 더 파고들어야 하는지" 판단할 수 있게 남겨둔다. url_type은 원본 후보의 종류를
            # 그대로 유지해서(덮어쓰지 않음) 디버깅용 정보를 보존한다.
            # candidate_url은 상태와 무관하게 항상 단일 URL이어야 한다(2026-07-29 결정, DB의
            # candidate_url엔 세미콜론 구분 원본 후보 목록을 절대 남기지 않음) — resolve_product가
            # 이미 성공/실패 어느 쪽이든 대표 URL 1개를 candidate_url 필드에 담아 반환한다.
            np['link_status'] = res.get('status') if res else None
            if res and res.get('candidate_url'):
                np['candidate_url'] = res['candidate_url'][:500]
            new_products.append(np)
        out.append({**item, 'products': new_products})
    # items+resolutions로 매번 전체를 다시 조립하므로, 재계산 후 특정 날짜에 남는 레코드가
    # 없어졌는데 옛 날짜 파일이 안 지워져 stale로 남는 걸 막기 위해 먼저 비운다.
    clear_json_dir(RESOLVED_DIR)
    dump_jsonl_sharded(RESOLVED_DIR, out, parent_date_key)


def main():
    if not DEEPSEEK_KEY:
        print('.env에 DEEPSEEK_KEY가 필요합니다.', file=sys.stderr)
        sys.exit(1)

    items = load_json_dir(LOAD_READY_DIR)
    resolutions = load_resolutions()

    pending = [
        (product_key(item['platform'], item['parent'], p['sort_order']), item, p)
        for item in items for p in item['products']
    ]
    pending = [(k, item, p) for k, item, p in pending if k not in resolutions]
    if len(sys.argv) > 1:
        pending = pending[:int(sys.argv[1])]

    print(f'해석 대상 {len(pending)}건 (이미 처리됨 {len(resolutions)}건) — 동시 워커 상한 {RESOLVE_CONCURRENCY}개, '
          f'브라우저 상한 {MAX_BROWSERS}개, requests 패스트패스 {"ON" if HTTP_FAST_PATH else "OFF"}')

    if pending:
        total = len(resolutions) + len(pending)

        def handle(ctx, row):
            key, item, p = row
            try:
                res = resolve_product(ctx.page, item['platform'], item['parent'], p)
            except Exception as e:
                res = {'status': 'error', 'final_url': None, 'note': str(e)[:160]}
            shown = res.get('final_url') or res.get('note', '')
            with ctx.lock:
                resolutions[key] = res
                done_n = len(resolutions)
                print(f'  [{done_n}/{total}] (w{ctx.worker_id}) {key} -> {res["status"]} {shown[:70]}', flush=True)
                # 결과 1건 = 파일 끝에 한 줄 추가(append)만 — 건수가 쌓여도 이 저장이 lock을
                # 오래 붙잡지 않는다(2026-07-27 실측/전환, common.append_jsonl 참고).
                append_jsonl(RESOLUTION_FILE, {**res, 'key': key})

        run_crawl_pool(pending, handle, concurrency=RESOLVE_CONCURRENCY,
                       item_delay=ITEM_DELAY, warn_hint='RESOLVE_CONCURRENCY')

    build_resolved_file(items, resolutions)
    by_status = {}
    for r in resolutions.values():
        by_status[r['status']] = by_status.get(r['status'], 0) + 1
    # 패스트패스 적중률을 남긴다 — "브라우저는 거의 안 뜬다"는 전제가 코드 주석에만 남고 실제로는
    # 무너져 있었던 게 성능 저하의 원인이었다(2026-08-01). 매 실행마다 실측치를 찍어둔다.
    hs = httpfetch_stats()
    if hs['tried']:
        print(f"requests 패스트패스: {hs['hit']}/{hs['tried']}건 적중 "
              f"({100 * hs['hit'] / hs['tried']:.1f}%) — 나머지는 브라우저로 폴백")
    print(f'누적 {len(resolutions)}건 — {by_status} -> {RESOLVED_DIR}/*.jsonl (날짜별)')


if __name__ == '__main__':
    main()
