#!/usr/bin/env python3
"""build_category_dataset.py가 만든 제품 목록(JSONL)을 LLM#4(04_category_classify)에 태워
각 제품에 category/subcategory를 붙인다. 체크포인트 저장이라 중간에 죽어도 이어서 실행 가능.
입력 파일의 줄 번호(row_id)로 완료 여부를 추적하므로, 입력 파일을 다시 만들지 않는 한
안전하게 재실행할 수 있다.

사용법:
    python3 scripts/classify_category.py                       # 기본 입출력 경로, 남은 것 전부
    LIMIT=20 python3 scripts/classify_category.py               # 이번 실행에 20건만(체크포인트 이어서)
    CONCURRENCY=8 python3 scripts/classify_category.py
    CONFIDENCE_THRESHOLD=0.5 python3 scripts/classify_category.py   # 기본값도 0.5
    python3 scripts/classify_category.py <입력.jsonl> <출력.jsonl>
결과: <출력.jsonl> (입력 레코드 + category/subcategory/confidence/reason/classify_error 필드,
    레코드 1개=1줄). LLM이 매긴 confidence가 CONFIDENCE_THRESHOLD 미만이면 category/subcategory를
    "미분류"로 덮어쓴다 — 원래 LLM이 골랐던 값은 llm_category/llm_subcategory에, 그 판단 이유는
    reason에 그대로 남겨둔다(왜 미분류로 빠졌는지 나중에 확인할 수 있게).
"""
import json
import os
import pathlib
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from common import CATEGORY_TAXONOMY, SUBCATEGORY_TO_CATEGORY, call_dify

IN_DEFAULT = pathlib.Path.home() / 'Desktop' / 'gonggu_category_input.jsonl'
OUT_DEFAULT = pathlib.Path.home() / 'Desktop' / 'gonggu_category_result.jsonl'

DIFY_KEY_CATEGORY = os.environ.get('DIFY_KEY_CATEGORY', '')
CONFIDENCE_THRESHOLD = float(os.environ.get('CONFIDENCE_THRESHOLD', '0.5'))

MAX_RETRY = 3
MAX_RETRY_429 = 10


def _is_429(e):
    return isinstance(e, requests.exceptions.HTTPError) and e.response is not None and e.response.status_code == 429


def _load_jsonl(path):
    if not path.exists():
        return []
    with open(path, encoding='utf-8') as f:
        return [json.loads(line) for line in f if line.strip()]


def classify_one(row):
    input_obj = {
        'product_name': row.get('product_name') or '',
        'title': row.get('title') or '',
        'description': row.get('description') or '',
    }
    last_err = None
    rate_limit_attempt = 0
    generic_attempt = 0
    while True:
        try:
            parsed = call_dify(input_obj, api_key=DIFY_KEY_CATEGORY, raw_inputs=True)
            llm_category, llm_subcategory = parsed.get('category'), parsed.get('subcategory')
            confidence = parsed.get('confidence')
            reason = parsed.get('reason')
            category, subcategory = llm_category, llm_subcategory
            # 가끔 LLM이 category 자리에 subcategory 문자열을 넣는 경우가 있어(예:
            # category="여행/캐리어") — 그 문자열이 우리 taxonomy의 하위카테고리와 정확히
            # 일치하면 원래 상위 카테고리로 되돌린다.
            if category not in CATEGORY_TAXONOMY and category in SUBCATEGORY_TO_CATEGORY:
                subcategory = subcategory or category
                category = SUBCATEGORY_TO_CATEGORY[category]
            # 13개 카테고리 안에 억지로 끼워맞춘 것뿐이라 LLM 스스로도 확신이 낮은 경우
            # (예: 여행용 캐리어처럼 애초에 이 체계에 안 맞는 제품) "미분류"로 뺀다 —
            # llm_category/llm_subcategory에 원래 판단을 남겨서 왜 빠졌는지 나중에 확인 가능.
            if isinstance(confidence, (int, float)) and confidence < CONFIDENCE_THRESHOLD:
                category, subcategory = '미분류', '미분류'
            return {**row, 'category': category, 'subcategory': subcategory, 'confidence': confidence,
                    'reason': reason, 'llm_category': llm_category, 'llm_subcategory': llm_subcategory,
                    'classify_error': None}
        except Exception as e:
            last_err = str(e)[:200]
            if _is_429(e):
                rate_limit_attempt += 1
                if rate_limit_attempt > MAX_RETRY_429:
                    return {**row, 'category': None, 'subcategory': None, 'classify_error': last_err}
                time.sleep(min(60, 5 * rate_limit_attempt))
                continue
            generic_attempt += 1
            if generic_attempt >= MAX_RETRY:
                return {**row, 'category': None, 'subcategory': None, 'classify_error': last_err}
            time.sleep(1.5 * generic_attempt)


def main():
    if not DIFY_KEY_CATEGORY:
        print('DIFY_KEY_CATEGORY 환경변수가 없음 — .env에 채워넣을 것', file=sys.stderr)
        sys.exit(1)

    in_path = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else IN_DEFAULT
    out_path = pathlib.Path(sys.argv[2]) if len(sys.argv) > 2 else OUT_DEFAULT

    rows = [{'row_id': i, **r} for i, r in enumerate(_load_jsonl(in_path))]
    prior = _load_jsonl(out_path)
    done = [r for r in prior if r.get('category') and not r.get('classify_error')]
    done_ids = {r['row_id'] for r in done}
    todo = [r for r in rows if r['row_id'] not in done_ids]

    limit = int(os.environ.get('LIMIT', '0')) or len(todo)
    todo = todo[:limit]
    concurrency = int(os.environ.get('CONCURRENCY', '4'))

    skipped = len(prior) - len(done)
    print(f'전체 {len(rows)} | 완료 {len(done)}{f" (재시도 대기 {skipped}건 제외)" if skipped else ""} | '
          f'이번 실행 {len(todo)}건 (동시 {concurrency})')

    # done만 남기고 실패건은 뺀 채로 출력 파일을 다시 쓴 다음, 이번 실행 결과는 한 줄씩 append —
    # 그래야 재시도 후 새 결과로 덮어써지고 같은 row_id가 중복으로 쌓이지 않는다.
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, 'w', encoding='utf-8') as f:
        for r in done:
            f.write(json.dumps(r, ensure_ascii=False) + '\n')

    REPORT_EVERY = 30
    lock = threading.Lock()
    total_done = len(done)
    ok_total = total_done
    err_total = 0
    batch_ok = 0
    batch_err = 0
    batch_err_samples = []

    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futures = {ex.submit(classify_one, r): r for r in todo}
        for i, fut in enumerate(as_completed(futures), 1):
            r = fut.result()
            with lock:
                with open(out_path, 'a', encoding='utf-8') as f:
                    f.write(json.dumps(r, ensure_ascii=False) + '\n')
                total_done += 1
                if r.get('classify_error'):
                    err_total += 1
                    batch_err += 1
                    batch_err_samples.append(str(r.get('classify_error'))[:80])
                else:
                    ok_total += 1
                    batch_ok += 1
                if i % REPORT_EVERY == 0 or i == len(todo):
                    print(f'  {i}/{len(todo)} 완료 — 이번 배치 성공 {batch_ok} / 실패 {batch_err} '
                          f'(누적 성공 {ok_total} / 실패 {err_total})')
                    for s in batch_err_samples[:3]:
                        print(f'    실패 예시: {s}')
                    batch_ok = batch_err = 0
                    batch_err_samples = []

    print(f'총 {total_done}건(성공 {ok_total} / 실패 {err_total}) -> {out_path}')


if __name__ == '__main__':
    main()
