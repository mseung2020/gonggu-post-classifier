"""워커 풀 실행/체크포인트/CLI 진입점. 실제 판단 로직은 core.py에 있고, 여기는 그걸
여러 상품에 대해 동시에·안전하게 돌리는 배관(plumbing)만 담당한다."""
import queue
import sys
import threading
import time

from playwright.sync_api import sync_playwright

from common import (LOAD_READY_DIR, RESOLVED_DIR, clear_json_dir, dump_json, dump_json_sharded,
                     load_json, load_json_dir, parent_date_key)

from .browser import new_context_page
from .config import (AUTH_STATE_FILE, DIFY_KEY_JUDGE, DIFY_KEY_PICK, ITEM_DELAY, RESOLUTION_FILE,
                      RESOLVE_CONCURRENCY)
from .core import resolve_product
from .matching import product_key


def load_resolutions():
    return load_json(RESOLUTION_FILE) if RESOLUTION_FILE.exists() else {}


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
            np['link_status'] = res.get('status') if res else None
            if res and res.get('status') == 'done' and res.get('final_url'):
                np['candidate_url'] = res['final_url'][:500]
            new_products.append(np)
        out.append({**item, 'products': new_products})
    # items+resolutions로 매번 전체를 다시 조립하므로, 재계산 후 특정 날짜에 남는 레코드가
    # 없어졌는데 옛 날짜 파일이 안 지워져 stale로 남는 걸 막기 위해 먼저 비운다.
    clear_json_dir(RESOLVED_DIR)
    dump_json_sharded(RESOLVED_DIR, out, parent_date_key)


def _resolve_worker(worker_id, work_q, resolutions, lock, total, save_auth_state):
    """워커 1개 = 독립된 Playwright 인스턴스+브라우저 1개. work_q에서 하나씩 꺼내 처리 —
    Playwright sync API는 스레드마다 별도 인스턴스를 쓰는 게 권장 방식이라 스레드끼리
    browser/page를 공유하지 않는다."""
    with sync_playwright() as pw:
        browser, ctx, page = new_context_page(pw)
        while True:
            try:
                key, item, p = work_q.get_nowait()
            except queue.Empty:
                break
            try:
                res = resolve_product(page, item['platform'], item['parent'], p)
            except Exception as e:
                res = {'status': 'error', 'final_url': None, 'note': str(e)[:160]}
            shown = res.get('final_url') or res.get('note', '')
            with lock:
                resolutions[key] = res
                done_n = len(resolutions)
                print(f'  [{done_n}/{total}] (w{worker_id}) {key} -> {res["status"]} {shown[:70]}', flush=True)
                if done_n % 10 == 0:
                    dump_json(RESOLUTION_FILE, resolutions)
            time.sleep(ITEM_DELAY)
        if save_auth_state:
            AUTH_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            ctx.storage_state(path=str(AUTH_STATE_FILE))
        browser.close()


def main():
    if not DIFY_KEY_PICK or not DIFY_KEY_JUDGE:
        print('.env에 DIFY_KEY_PICK / DIFY_KEY_JUDGE가 필요합니다.', file=sys.stderr)
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

    n_workers = max(1, min(RESOLVE_CONCURRENCY, len(pending) or 1))
    print(f'해석 대상 {len(pending)}건 (이미 처리됨 {len(resolutions)}건) — 동시 워커 {n_workers}개')

    if pending:
        total = len(resolutions) + len(pending)
        work_q = queue.Queue()
        for row in pending:
            work_q.put(row)
        lock = threading.Lock()
        threads = [
            threading.Thread(target=_resolve_worker, args=(wid, work_q, resolutions, lock, total, wid == 0))
            for wid in range(n_workers)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        dump_json(RESOLUTION_FILE, resolutions)

    build_resolved_file(items, resolutions)
    by_status = {}
    for r in resolutions.values():
        by_status[r['status']] = by_status.get(r['status'], 0) + 1
    print(f'누적 {len(resolutions)}건 — {by_status} -> {RESOLVED_DIR}/*.json (날짜별)')


if __name__ == '__main__':
    main()
