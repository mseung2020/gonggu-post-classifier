#!/usr/bin/env python3
"""LLM 사용량 집계 — common.call_llm이 매 호출마다 남기는 data/output/llm_usage.jsonl을
날짜별·모델별 토큰 합계로 집계해서 보여준다. 일일 퀘스트를 시작하기 전에 이 파일을
지워두면(또는 그냥 두고 DATE로 날짜를 지정해도 됨) 퀘스트가 끝난 뒤 이 스크립트로 그날 하루
합계를 확인할 수 있다.

⚠ 단가(원/달러)는 코드에 하드코딩하지 않는다. 이 파이프라인이 쓰는 모델명(deepseek-v4-pro
등)이 DeepSeek 공개 요금표의 표준 모델명과 다를 수 있어(내부 게이트웨이/별칭 가능성), 잘못된
단가를 하드코딩해서 틀린 비용을 보여주는 것보다 토큰 수만 정확히 보여주는 쪽을 택했다. 실제
단가를 알면(DeepSeek 대시보드 등에서 확인) 환경변수로 넣어서 비용까지 계산할 수 있다.

사용법:
    python3 scripts/llm_usage_report.py                     # 오늘 날짜
    DATE=2026-08-02 python3 scripts/llm_usage_report.py     # 특정 날짜
    PRICE_INPUT_PER_1M=0.14 PRICE_OUTPUT_PER_1M=0.28 PRICE_CACHE_HIT_PER_1M=0.014 \\
        python3 scripts/llm_usage_report.py                 # 단가를 알면 비용도 같이 계산
"""
import datetime
import json
import os
from collections import defaultdict

from common import LLM_USAGE_FILE


def _load_entries(date_str):
    if not LLM_USAGE_FILE.exists():
        return []
    out = []
    with open(LLM_USAGE_FILE, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if rec.get('ts', '').startswith(date_str):
                out.append(rec)
    return out


def main():
    date_str = os.environ.get('DATE') or datetime.date.today().isoformat()
    entries = _load_entries(date_str)
    if not entries:
        print(f'{date_str} 기록 없음 ({LLM_USAGE_FILE} 확인)')
        return

    by_model = defaultdict(lambda: {'calls': 0, 'prompt': 0, 'completion': 0, 'total': 0,
                                     'cache_hit': 0})
    for r in entries:
        m = by_model[r.get('model') or '?']
        m['calls'] += 1
        m['prompt'] += r.get('prompt_tokens') or 0
        m['completion'] += r.get('completion_tokens') or 0
        m['total'] += r.get('total_tokens') or 0
        m['cache_hit'] += r.get('cache_hit_tokens') or 0

    price_in = os.environ.get('PRICE_INPUT_PER_1M')
    price_out = os.environ.get('PRICE_OUTPUT_PER_1M')
    price_cache_hit = os.environ.get('PRICE_CACHE_HIT_PER_1M')
    has_price = bool(price_in and price_out)

    print(f'=== {date_str} LLM 사용량 (호출 {len(entries)}건) ===')
    grand_tokens, grand_cost = 0, 0.0
    for model, m in sorted(by_model.items()):
        grand_tokens += m['total']
        line = (f"{model}: 호출 {m['calls']}건, 총 {m['total']:,} 토큰 "
                f"(입력 {m['prompt']:,} / 출력 {m['completion']:,}"
                + (f" / 캐시히트 {m['cache_hit']:,}" if m['cache_hit'] else '') + ')')
        if has_price:
            billable_prompt = m['prompt'] - m['cache_hit']
            cost = (billable_prompt * float(price_in) + m['completion'] * float(price_out)
                    + (m['cache_hit'] * float(price_cache_hit) if price_cache_hit else 0)) / 1_000_000
            grand_cost += cost
            line += f' -> ${cost:.4f}'
        print(' ', line)

    print(f'합계: {grand_tokens:,} 토큰' + (f' / ${grand_cost:.4f}' if has_price else ''))
    if not has_price:
        print('(단가를 몰라서 비용은 생략 — PRICE_INPUT_PER_1M/PRICE_OUTPUT_PER_1M'
              '[/PRICE_CACHE_HIT_PER_1M] 환경변수로 넣으면 비용까지 계산됩니다.)')


if __name__ == '__main__':
    main()
