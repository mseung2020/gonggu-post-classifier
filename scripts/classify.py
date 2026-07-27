#!/usr/bin/env python3
"""2단계: posts_raw.json의 각 포스트를 LLM#1(01_gonggu_classify)에 태워 공구 여부/상품명 배열/
날짜/링크위치를 뽑는다. 체크포인트 저장이라 중간에 죽어도 이어서 실행 가능.

사용법:
    CONCURRENCY=4 python3 scripts/classify.py            # 남은 것 전부
    LIMIT=500 python3 scripts/classify.py                # 이번 실행에 500건만 (체크포인트 이어서)
    PLATFORM=yt LIMIT=500 python3 scripts/classify.py    # ig/yt 중 하나만 골라서 500건
결과: data/02_classified/<발행일>.json (원본 포스트 + classification 필드 추가, 날짜별)
"""
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from common import (CLASSIFIED_DIR, DIFY_KEY, RAW_DIR, call_dify, dump_json, load_json_dir,
                     post_date_key)

MAX_RETRY = 3
# 429(레이트리밋)는 코드 버그가 아니라 "잠깐 기다리면 반드시 풀리는" 상태라 훨씬 길게/많이
# 재시도한다 — 짧게 3번만 시도하고 포기하면 대량 동시 처리 시 전부 영구 실패로 남는다.
MAX_RETRY_429 = 10


def _key(post):
    native_id = post.get('post_id') if post['platform'] == 'ig' else post.get('video_id')
    return f"{post['platform']}:{native_id}"


def _is_429(e):
    return isinstance(e, requests.exceptions.HTTPError) and e.response is not None and e.response.status_code == 429


def classify_one(post):
    pub_date = post.get('publish_date') if post['platform'] == 'ig' else post.get('publishDate')
    input_obj = {
        'description': post.get('description') or '',
        'publish_date': pub_date or '',
        'creator_description': post.get('creator_description') or '',
    }
    last_err = None
    rate_limit_attempt = 0
    generic_attempt = 0
    while True:
        try:
            parsed = call_dify(input_obj)
            return {**post, 'classification': parsed, 'classification_error': None}
        except Exception as e:
            last_err = str(e)[:200]
            if _is_429(e):
                rate_limit_attempt += 1
                if rate_limit_attempt > MAX_RETRY_429:
                    return {**post, 'classification': None, 'classification_error': last_err}
                time.sleep(min(60, 5 * rate_limit_attempt))
                continue
            generic_attempt += 1
            if generic_attempt >= MAX_RETRY:
                return {**post, 'classification': None, 'classification_error': last_err}
            time.sleep(1.5 * generic_attempt)


def main():
    if not DIFY_KEY:
        print('DIFY_KEY 환경변수가 없음 — .env에 채워넣을 것', file=sys.stderr)
        sys.exit(1)

    posts = load_json_dir(RAW_DIR)
    prior = load_json_dir(CLASSIFIED_DIR)
    # classification_error가 남은 건(예: 예전에 429로 실패)은 "완료"로 치지 않는다 — 그래야
    # 다음 실행에서 todo에 다시 들어가 자동 재시도된다. done이 아닌 건 버킷에도 안 넣어서,
    # 재시도 후 새 결과로 덮어써지게 한다(안 그러면 같은 키가 중복으로 쌓임).
    done = [r for r in prior if r.get('classification') and not r.get('classification_error')]
    done_keys = {_key(r) for r in done}
    todo = [p for p in posts if _key(p) not in done_keys]

    platform = os.environ.get('PLATFORM')  # 'ig' 또는 'yt'만 지정하면 그 플랫폼만 골라서 처리
    if platform:
        todo = [p for p in todo if p['platform'] == platform]

    limit = int(os.environ.get('LIMIT', '0')) or len(todo)
    todo = todo[:limit]

    concurrency = int(os.environ.get('CONCURRENCY', '4'))
    scope = f'platform={platform} ' if platform else ''
    skipped = len(prior) - len(done)
    print(f'전체 {len(posts)} | 완료 {len(done)}{f" (재시도 대기 {skipped}건 제외)" if skipped else ""} | '
          f'이번 실행 {scope}{len(todo)}건 (동시 {concurrency})')

    # 날짜별로 쪼갠 김에 체크포인트도 "이번에 뭔가 새로 들어온 날짜 파일만" 다시 쓴다 —
    # 안 그러면 날짜별로 나눈 보람 없이 매번 전체를 다시 훑어 쓰게 된다.
    SAVE_EVERY = 30
    buckets = {}
    for r in done:
        buckets.setdefault(post_date_key(r), []).append(r)
    dirty_dates = set()

    def _flush():
        for d in dirty_dates:
            dump_json(CLASSIFIED_DIR / f'{d}.json', buckets[d], indent=None)
        dirty_dates.clear()

    total_done = len(done)
    ok_total = total_done
    err_total = 0
    batch_ok = 0
    batch_err = 0
    batch_err_samples = []

    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futures = {ex.submit(classify_one, p): p for p in todo}
        for i, fut in enumerate(as_completed(futures), 1):
            r = fut.result()
            d = post_date_key(r)
            buckets.setdefault(d, []).append(r)
            dirty_dates.add(d)
            total_done += 1
            if r.get('classification_error'):
                err_total += 1
                batch_err += 1
                batch_err_samples.append(str(r.get('classification_error'))[:80])
            else:
                ok_total += 1
                batch_ok += 1
            if i % SAVE_EVERY == 0 or i == len(todo):
                _flush()
                print(f'  {i}/{len(todo)} 완료 (저장됨) — 이번 배치 성공 {batch_ok} / 실패 {batch_err} '
                      f'(누적 성공 {ok_total} / 실패 {err_total})')
                for s in batch_err_samples[:3]:  # 실패가 났으면 사유를 바로 보여줘서 429 재발 같은 걸 빨리 눈치채게
                    print(f'    실패 예시: {s}')
                batch_ok = batch_err = 0
                batch_err_samples = []

    _flush()
    print(f'총 {total_done}건(성공 {ok_total} / 실패 {err_total}) -> {CLASSIFIED_DIR}/*.json (날짜별)')


if __name__ == '__main__':
    main()
