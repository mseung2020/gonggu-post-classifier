#!/usr/bin/env python3
"""임시 진단 스크립트 — 실제 파이프라인 체크포인트는 건드리지 않고, 랜덤 샘플로
classify -> transform -> resolve_links를 한 번에 돌려서 결과를 data/output/_diag_result.json에
남긴다. 사람이 (또는 Claude가) 결과를 하나하나 읽고 진단하기 위한 용도.

사용법:
    python3 scripts/_diag_sample.py            # 포스트 300개 랜덤 샘플 -> 상품 50개 랜덤 샘플
    python3 scripts/_diag_sample.py 500 80     # 포스트 500개, 상품 80개
"""
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

from classify import classify_one
from common import DIFY_KEY, RAW_FILE, ROOT, dump_json, load_json
from resolve_links import (AUTH_STATE_FILE, DIFY_KEY_JUDGE, DIFY_KEY_PICK, UA, product_key,
                            resolve_product)
from transform import transform_one

DIAG_FILE = ROOT / 'data/output/_diag_result.json'

POST_N = int(sys.argv[1]) if len(sys.argv) > 1 else 300
PRODUCT_N = int(sys.argv[2]) if len(sys.argv) > 2 else 50


def main():
    if not DIFY_KEY or not DIFY_KEY_PICK or not DIFY_KEY_JUDGE:
        print('DIFY_KEY / DIFY_KEY_PICK / DIFY_KEY_JUDGE가 .env에 모두 필요합니다.', file=sys.stderr)
        sys.exit(1)

    posts = load_json(RAW_FILE)
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
    print(f'상품 {len(picked)}개 랜덤 샘플 -> 링크 해석 중...')

    results = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=['--disable-blink-features=AutomationControlled'])
        ctx_kwargs = dict(user_agent=UA, locale='ko-KR', viewport={'width': 1360, 'height': 900},
                           extra_http_headers={'Accept-Language': 'ko-KR,ko;q=0.9,en;q=0.8'})
        if AUTH_STATE_FILE.exists():
            ctx_kwargs['storage_state'] = str(AUTH_STATE_FILE)
        ctx = browser.new_context(**ctx_kwargs)
        Stealth(navigator_platform_override='MacIntel',
                navigator_languages_override=('ko-KR', 'ko')).apply_stealth_sync(ctx)
        page = ctx.new_page()

        for i, (platform, parent, product, raw_post) in enumerate(picked, 1):
            try:
                res = resolve_product(page, platform, parent, product)
            except Exception as e:
                res = {'status': 'error', 'final_url': None, 'note': str(e)[:200]}
            results.append({
                'key': product_key(platform, parent, product['sort_order']),
                'description': raw_post.get('description') or raw_post.get('video_description'),
                'creator_description': raw_post.get('creator_description'),
                'product': product,
                'classification_note': parent.get('classification_note'),
                'resolution': res,
            })
            print(f'  [{i}/{len(picked)}] {results[-1]["key"]} -> {res["status"]}')
            time.sleep(2)

        browser.close()

    dump_json(DIAG_FILE, results)
    by_status = {}
    for r in results:
        by_status[r['resolution']['status']] = by_status.get(r['resolution']['status'], 0) + 1
    print(f'\n완료: {by_status} -> {DIAG_FILE}')


if __name__ == '__main__':
    main()
