#!/usr/bin/env python3
"""임시 진단 스크립트 — 실제 파이프라인 체크포인트는 건드리지 않고, 랜덤 샘플로
classify -> transform -> resolve_links를 한 번에 돌려서 결과를 data/output/_diag_result.json에
남긴다. 사람이 (또는 Claude가) 결과를 하나하나 읽고 진단하기 위한 용도.

사용법:
    python3 scripts/_diag_sample.py            # 포스트 300개 랜덤 샘플 -> 상품 50개 랜덤 샘플
    python3 scripts/_diag_sample.py 500 80     # 포스트 500개, 상품 80개
"""
import queue
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from playwright.sync_api import sync_playwright

from classify import classify_one
from common import DEEPSEEK_KEY, RAW_DIR, ROOT, dump_json, load_json_dir
from resolve_links.browser import LazyPage
from resolve_links.config import RESOLVE_CONCURRENCY
from resolve_links.core import resolve_product
from resolve_links.matching import product_key
from transform import transform_one

DIAG_FILE = ROOT / 'data/output/_diag_result.json'

POST_N = int(sys.argv[1]) if len(sys.argv) > 1 else 300
PRODUCT_N = int(sys.argv[2]) if len(sys.argv) > 2 else 50


def main():
    if not DEEPSEEK_KEY:
        print('DEEPSEEK_KEY가 .env에 필요합니다.', file=sys.stderr)
        sys.exit(1)

    posts = load_json_dir(RAW_DIR)
    sample = random.sample(posts, min(POST_N, len(posts)))
    print(f'포스트 {len(sample)}건 랜덤 샘플 -> LLM#1 분류 중...')

    classified = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = {ex.submit(classify_one, p): p for p in sample}
        for i, fut in enumerate(as_completed(futures), 1):
            classified.append(fut.result())
            if i % 50 == 0:
                print(f'  분류 {i}/{len(sample)}')

    candidates = []  # [(platform, parent, product, raw_post), ...]
    reject_n = 0
    for post in classified:
        parent, products, reject_reason = transform_one(post)
        if reject_reason:
            reject_n += 1
            continue
        for p in products:
            if p.get('candidate_url'):
                candidates.append((post['platform'], parent, p, post))

    print(f'분류 {len(classified)}건 -> 게이트 통과 {len(classified) - reject_n}건 -> '
          f'candidate_url 있는 상품 {len(candidates)}개')

    picked = random.sample(candidates, min(PRODUCT_N, len(candidates)))
    n_workers = max(1, min(RESOLVE_CONCURRENCY, len(picked) or 1))
    print(f'상품 {len(picked)}개 랜덤 샘플 -> 링크 해석 중... (동시 워커 {n_workers}개)')

    results = []
    lock = threading.Lock()
    work_q = queue.Queue()
    for row in picked:
        work_q.put(row)

    def _diag_worker(worker_id):
        with sync_playwright() as pw:
            # 워커마다 무조건 브라우저를 띄우면 RESOLVE_CONCURRENCY를 크게 준 채로 이 진단
            # 스크립트를 돌릴 때 그 수만큼 크롬이 뜬다 — LazyPage로 지연 생성 + MAX_BROWSERS
            # 제한을 건다(resolve_links/browser.py 참고, runner.py/rescan_inprogress.py와 동일).
            page = LazyPage(pw)
            while True:
                try:
                    platform, parent, product, raw_post = work_q.get_nowait()
                except queue.Empty:
                    break
                try:
                    res = resolve_product(page, platform, parent, product)
                except Exception as e:
                    res = {'status': 'error', 'final_url': None, 'note': str(e)[:200]}
                row = {
                    'key': product_key(platform, parent, product['sort_order']),
                    'description': raw_post.get('description') or raw_post.get('video_description'),
                    'creator_description': raw_post.get('creator_description'),
                    'product': product,
                    'classification_note': parent.get('classification_note'),
                    'resolution': res,
                }
                with lock:
                    results.append(row)
                    print(f'  [{len(results)}/{len(picked)}] (w{worker_id}) {row["key"]} -> {res["status"]}',
                          flush=True)
                page.release_if_contended()
                time.sleep(2)
            page.close()

    threads = [threading.Thread(target=_diag_worker, args=(wid,)) for wid in range(n_workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    dump_json(DIAG_FILE, results)
    by_status = {}
    for r in results:
        by_status[r['resolution']['status']] = by_status.get(r['resolution']['status'], 0) + 1
    print(f'\n완료: {by_status} -> {DIAG_FILE}')


if __name__ == '__main__':
    main()
