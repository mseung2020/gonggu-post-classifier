#!/usr/bin/env python3
"""2단계: posts_raw.json의 각 포스트를 LLM#1(01_gonggu_classify)에 태워 공구 여부/상품명 배열/
날짜/링크위치를 뽑는다. 체크포인트 저장이라 중간에 죽어도 이어서 실행 가능.

사용법:
    CONCURRENCY=4 python3 scripts/classify.py
결과: data/output/classified.json (원본 포스트 + classification 필드 추가)
"""
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from common import CLASSIFIED_FILE, DIFY_KEY, RAW_FILE, call_dify, dump_json, load_json

MAX_RETRY = 3


def _key(post):
    native_id = post.get('post_id') if post['platform'] == 'ig' else post.get('video_id')
    return f"{post['platform']}:{native_id}"


def classify_one(post):
    pub_date = post.get('publish_date') if post['platform'] == 'ig' else post.get('publishDate')
    input_obj = {
        'description': post.get('description') or '',
        'publish_date': pub_date or '',
        'creator_description': post.get('creator_description') or '',
    }
    last_err = None
    for attempt in range(1, MAX_RETRY + 1):
        try:
            parsed = call_dify(input_obj)
            return {**post, 'classification': parsed, 'classification_error': None}
        except Exception as e:
            last_err = str(e)[:200]
            time.sleep(1.5 * attempt)
    return {**post, 'classification': None, 'classification_error': last_err}


def main():
    if not DIFY_KEY:
        print('DIFY_KEY 환경변수가 없음 — .env에 채워넣을 것', file=sys.stderr)
        sys.exit(1)

    posts = load_json(RAW_FILE)
    prior = load_json(CLASSIFIED_FILE) if CLASSIFIED_FILE.exists() else []
    done_keys = {_key(r) for r in prior}
    todo = [p for p in posts if _key(p) not in done_keys]

    concurrency = 4
    print(f'전체 {len(posts)} | 완료 {len(prior)} | 이번 실행 {len(todo)}건 (동시 {concurrency})')

    results = list(prior)
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futures = {ex.submit(classify_one, p): p for p in todo}
        for i, fut in enumerate(as_completed(futures), 1):
            results.append(fut.result())
            if i % 10 == 0 or i == len(todo):
                dump_json(CLASSIFIED_FILE, results)
                print(f'  {i}/{len(todo)} 완료 (저장됨)')

    dump_json(CLASSIFIED_FILE, results)
    print(f'총 {len(results)}건 -> {CLASSIFIED_FILE}')


if __name__ == '__main__':
    main()
