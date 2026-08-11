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
import re
import sys

from gonggu.common import (DEEPSEEK_KEY, LOAD_READY_DIR, RESOLVED_DIR, acquire_lock, append_jsonl,
                     clear_json_dir, dump_jsonl_sharded, load_json_dir, load_jsonl, parent_date_key)
from gonggu.crawl_pool import run_crawl_pool

from .config import (HTTP_FAST_PATH, ITEM_DELAY, ITEM_DELAY_SMART, MAX_BROWSERS,
                     RESOLUTION_FILE, RESOLVE_CONCURRENCY)
from .httpfetch import stats as httpfetch_stats
from .core import resolve_product
from .links import cached_linkbio_data, normalize_url
from .matching import product_key


def _dump_linkbio(items):
    """resolve 중 파싱해 캐시에 남은 인포크 허브 원본을 포스트별로 모아 게시일별 JSONL로 저장한다.
    ⚠ 재크롤 없음 — resolve가 이미 파싱한 캐시(cached_linkbio_data)만 꺼내 쓰므로, crawl_linkbio의
    독립 크롤을 데일리에서 대체한다. 이번 실행에 인포크가 실제로 파싱된 포스트만 기록된다(증분).
    이미 적재된 옛 포스트의 소급은 여전히 standalone `python3 -m gonggu.crawl_linkbio`가 담당."""
    from gonggu.crawl_linkbio import OUT_DIR, extract_inpock_hubs  # 지연 import(순환참조 회피)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    written = 0
    for item in items:
        parent, code = item.get('parent') or {}, item.get('platform')
        nid = parent.get('post_id') if code == 'ig' else parent.get('video_id')
        if not nid:
            continue
        date = str(parent.get('publish_date') or parent.get('publishDate') or '')[:10]
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', date or ''):
            date = 'unknown'
        cand_text = ' '.join((p.get('candidate_url') or '') for p in item.get('products') or [])
        linkbio = []
        for h in extract_inpock_hubs(cand_text):
            data = cached_linkbio_data(normalize_url(h)) or cached_linkbio_data(h)
            if data:
                linkbio.append({'hub_url': h, 'parsed': data, 'error': None})
        if linkbio:
            append_jsonl(OUT_DIR / f'{date}.jsonl',
                         {'key': f'{code}:{nid}', 'platform': code, 'post_id': nid,
                          'publish_date': date, 'hub_urls': [x['hub_url'] for x in linkbio],
                          'linkbio': linkbio})
            written += 1
    if written:
        print(f'  인포크 파싱본 저장: {written}개 포스트 -> data/linkbio/<게시일>.jsonl (재크롤 없음)')


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
            # link_note = 이 상태가 왜 나왔는지(LLM#3의 reason 또는 "후보 링크 없음/로그인월
            # 차단/상품명 너무 일반적" 같은 결정적 사유). 이미 core.py가 상품별로 만들어 두는
            # 것을 DB 상품 행까지 실어 나른다(파일에만 있던 걸 건바이건으로 보존). VARCHAR(255)
            # 안전하게 자른다(core.py에서 이미 짧게 잘리지만 방어적).
            note = res.get('note') if res else None
            np['link_note'] = note[:255] if note else None
            new_products.append(np)
        out.append({**item, 'products': new_products})
    # items+resolutions로 매번 전체를 다시 조립하므로, 재계산 후 특정 날짜에 남는 레코드가
    # 없어졌는데 옛 날짜 파일이 안 지워져 stale로 남는 걸 막기 위해 먼저 비운다.
    clear_json_dir(RESOLVED_DIR)
    dump_jsonl_sharded(RESOLVED_DIR, out, parent_date_key)


def main():
    acquire_lock('resolve_links')
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
                       item_delay=ITEM_DELAY, delay_only_after_browser=ITEM_DELAY_SMART,
                       warn_hint='RESOLVE_CONCURRENCY')

    build_resolved_file(items, resolutions)
    _dump_linkbio(items)   # 인포크 파싱본을 게시일별 JSON으로(재크롤 없이 캐시에서)
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
