#!/usr/bin/env python3
"""LLM 사용량 집계 — common.call_llm이 매 호출마다 남기는 data/output/llm_usage.jsonl을
날짜별·모델별 토큰 합계와 비용(단가를 아는 모델만)으로 집계해서 보여준다. gonggu.daily가
퀘스트 마지막에 자동 실행하므로 보통은 따로 돌릴 일이 없다.

비용 계산: 아래 MODEL_PRICES에 단가가 등록된 모델은 자동으로 달러 비용까지 계산한다.
등록 안 된 모델은 토큰 수만 보여준다(틀린 단가로 틀린 비용을 보여주는 것보다 낫다는
원칙 — 예전 주석 참고). 환경변수(PRICE_INPUT_PER_1M 등)를 주면 **모든 모델에** 그 단가를
강제 적용한다(예전 동작 그대로 — 단가표보다 우선).

사용법:
    python3 -m gonggu.llm_usage_report                     # 오늘 날짜
    DATE=2026-08-02 python3 -m gonggu.llm_usage_report     # 특정 날짜
    PRICE_INPUT_PER_1M=0.14 PRICE_OUTPUT_PER_1M=0.28 PRICE_CACHE_HIT_PER_1M=0.0028 \\
        python3 -m gonggu.llm_usage_report                 # 단가 강제 지정(전 모델 공통)
"""
import datetime
import json
import os
from collections import defaultdict

from gonggu.common import LLM_USAGE_FILE

# 모델별 공식 단가($ / 1M tokens). 2026-08-06 명승님이 확인해준 DeepSeek 공식 단가.
# 'input'은 캐시 미스 기준(청구는 miss분 × input + hit분 × cache_hit + 출력 × output).
# 프로(deepseek-v4-pro) 등 다른 모델 단가를 확인하면 여기에 한 줄 추가하면 된다.
MODEL_PRICES = {
    'deepseek-v4-flash': {'input': 0.14, 'output': 0.28, 'cache_hit': 0.0028},
}


def price_for(model):
    """이 모델에 적용할 단가 dict 또는 None. 환경변수가 있으면 전 모델 공통으로 우선 적용."""
    env_in, env_out = os.environ.get('PRICE_INPUT_PER_1M'), os.environ.get('PRICE_OUTPUT_PER_1M')
    if env_in and env_out:
        return {'input': float(env_in), 'output': float(env_out),
                'cache_hit': float(os.environ.get('PRICE_CACHE_HIT_PER_1M', '0'))}
    return MODEL_PRICES.get(model)


def cost_usd(totals, prices):
    """모델 합계(prompt/completion/cache_hit 토큰) × 단가 → 달러 비용."""
    billable_miss = totals['prompt'] - totals['cache_hit']
    return (billable_miss * prices['input']
            + totals['cache_hit'] * prices['cache_hit']
            + totals['completion'] * prices['output']) / 1_000_000


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

    print(f'=== {date_str} LLM 사용량 (호출 {len(entries)}건) ===')
    grand_tokens, grand_cost, unknown_models = 0, 0.0, []
    for model, m in sorted(by_model.items()):
        grand_tokens += m['total']
        hit_pct = 100 * m['cache_hit'] / m['prompt'] if m['prompt'] else 0
        line = (f"{model}: 호출 {m['calls']:,}건, 총 {m['total']:,} 토큰 "
                f"(입력 {m['prompt']:,} / 출력 {m['completion']:,}"
                + (f" / 캐시히트 {m['cache_hit']:,} = {hit_pct:.0f}%" if m['cache_hit'] else '') + ')')
        prices = price_for(model)
        if prices:
            cost = cost_usd(m, prices)
            grand_cost += cost
            line += f' -> ${cost:.4f}'
        else:
            unknown_models.append(model)
        print(' ', line)

    summary = f'합계: {grand_tokens:,} 토큰'
    if grand_cost:
        summary += f' / ${grand_cost:.4f}'
        if unknown_models:
            summary += f' (단가 미상 모델 제외: {", ".join(unknown_models)})'
    print(summary)
    if unknown_models and not grand_cost:
        print(f'(단가 미상 모델({", ".join(unknown_models)})이라 비용 생략 — gonggu/llm_usage_report.py의 '
              f'MODEL_PRICES에 단가를 추가하거나 PRICE_INPUT_PER_1M 등으로 지정)')


if __name__ == '__main__':
    main()
