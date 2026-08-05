#!/usr/bin/env python3
"""신규 모듈(기존 파이프라인과 완전 독립) 2/2 — fetch_yt_ppl.py가 저장한
data/01_raw_yt_ppl/의 각 영상을 이 파일 전용 프롬프트(prompts.YT_PPL_GONGGU_SYSTEM)로
"PPL이지만 사실상 그룹특가인지" 판별한다.

classify.py(LLM#1, GONGGU_CLASSIFY_SYSTEM)는 이 목적으로 재사용하지 않는다 — 이미 검증된
그 판정 로직/파일에 전혀 손대지 않기 위해 fetch도, 프롬프트도, 스크립트도 완전히 분리했다.
결과는 classify.py와 같은 data/02_classified/<발행일>.jsonl에 같은 레코드 스키마로
append하므로 transform.py부터는 무수정으로 이 결과를 그대로 처리한다. 체크포인트 저장이라
중간에 죽어도 이어서 실행 가능.

사용법:
    CONCURRENCY=100 python3 scripts/classify_yt_ppl.py
    LIMIT=20 python3 scripts/classify_yt_ppl.py          # 소량 스모크 테스트
결과: data/02_classified/<발행일>.jsonl (classify.py와 동일 디렉터리를 공유하지만 서로 다른
    video_id만 다루므로 충돌 없이 append됨)
"""
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from gonggu.common import CLASSIFIED_DIR, DEEPSEEK_KEY, ROOT, append_jsonl, call_llm, load_json_dir, post_date_key
from gonggu.prompts import YT_PPL_GONGGU_SYSTEM, build_yt_ppl_gonggu_user

RAW_DIR_YT_PPL = ROOT / 'data/01_raw_yt_ppl'

MAX_RETRY = 3
# 429(레이트리밋)는 코드 버그가 아니라 잠깐 기다리면 풀리는 상태라 classify.py와 동일하게
# 길게/많이 재시도한다.
MAX_RETRY_429 = 10


def _key(post):
    return f"yt:{post['video_id']}"


def _is_429(e):
    return isinstance(e, requests.exceptions.HTTPError) and e.response is not None and e.response.status_code == 429


def classify_one(post):
    user_message = build_yt_ppl_gonggu_user(
        title=post.get('title') or '',
        description=post.get('description') or '',
        brand_name=post.get('brand_name'),
        sponsored_type=post.get('sponsored_type'),
    )
    last_err = None
    rate_limit_attempt = 0
    generic_attempt = 0
    while True:
        try:
            parsed = call_llm(YT_PPL_GONGGU_SYSTEM, user_message)
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
    if not DEEPSEEK_KEY:
        print('DEEPSEEK_KEY 환경변수가 없음 — .env에 채워넣을 것', file=sys.stderr)
        sys.exit(1)

    posts = load_json_dir(RAW_DIR_YT_PPL)

    # 체크포인트: data/02_classified/를 읽어 이미 처리된(성공한) video_id는 스킵한다.
    # classify.py도 같은 디렉터리에 쓰지만 그쪽은 fetch_source.py가 만든(공구 키워드 있는)
    # video_id만 다루고, 이 스크립트는 fetch_yt_ppl.py가 만든(공구 키워드 없는) video_id만
    # 다루므로 두 체크포인트가 서로의 결과를 되짚어 재처리하는 일은 없다.
    prior = load_json_dir(CLASSIFIED_DIR)
    done = [r for r in prior if r.get('platform') == 'yt' and r.get('classification') and not r.get('classification_error')]
    done_keys = {_key(r) for r in done if r.get('video_id')}
    todo_all = [p for p in posts if _key(p) not in done_keys]
    already_done = len(posts) - len(todo_all)  # LIMIT 적용 전에 계산해둔다

    limit = int(os.environ.get('LIMIT', '0')) or len(todo_all)
    todo = todo_all[:limit]

    concurrency = int(os.environ.get('CONCURRENCY', '4'))
    print(f'전체 {len(posts)} | 완료 {already_done} | 이번 실행 {len(todo)}건 (동시 {concurrency})')

    REPORT_EVERY = 30
    lock = threading.Lock()
    total_done = already_done
    ok_total = 0
    err_total = 0
    gonggu_total = 0
    batch_ok = 0
    batch_err = 0
    batch_err_samples = []

    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futures = {ex.submit(classify_one, p): p for p in todo}
        for i, fut in enumerate(as_completed(futures), 1):
            r = fut.result()
            with lock:
                append_jsonl(CLASSIFIED_DIR / f'{post_date_key(r)}.jsonl', r)
                total_done += 1
                if r.get('classification_error'):
                    err_total += 1
                    batch_err += 1
                    batch_err_samples.append(str(r.get('classification_error'))[:80])
                else:
                    ok_total += 1
                    batch_ok += 1
                    if (r.get('classification') or {}).get('is_gonggu'):
                        gonggu_total += 1
                if i % REPORT_EVERY == 0 or i == len(todo):
                    print(f'  {i}/{len(todo)} 완료 — 이번 배치 성공 {batch_ok} / 실패 {batch_err} '
                          f'(누적 성공 {ok_total} / 실패 {err_total} / 공구판정 {gonggu_total})')
                    for s in batch_err_samples[:3]:
                        print(f'    실패 예시: {s}')
                    batch_ok = batch_err = 0
                    batch_err_samples = []

    print(f'총 {total_done}건(성공 {ok_total} / 실패 {err_total} / 공구판정 {gonggu_total}) '
          f'-> {CLASSIFIED_DIR}/*.jsonl (날짜별, classify.py와 공유)')


if __name__ == '__main__':
    main()
