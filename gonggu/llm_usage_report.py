#!/usr/bin/env python3
"""LLM 사용량 집계 — common.call_llm이 매 호출마다 남기는 data/output/llm_usage.jsonl을
날짜별·모델별 토큰 합계와 비용(단가를 아는 모델만)으로 집계해서 보여준다. gonggu.daily가
퀘스트 마지막에 자동 실행하므로 보통은 따로 돌릴 일이 없다.

비용 계산: 아래 MODEL_PRICES에 단가가 등록된 모델은 자동으로 달러 비용까지 계산한다.
등록 안 된 모델은 토큰 수만 보여준다(틀린 단가로 틀린 비용을 보여주는 것보다 낫다는
원칙 — 예전 주석 참고). 환경변수(PRICE_INPUT_PER_1M 등)를 주면 **모든 모델에** 그 단가를
강제 적용한다(예전 동작 그대로 — 단가표보다 우선, 이때는 피크/오프피크 구분 없이 그 값 하나).

⚠ 2026-08-19 요금 개편 — 피크/오프피크가 생겼다(PEAK_HOURS_UTC 참고). 호출 하나하나가 UTC 몇
시에 났느냐로 단가가 2배 갈리므로, 예전처럼 "하루치를 모아서 단가 한 번 곱하기"로는 못 센다.
그래서 집계를 호출 시각별 피크 여부로 나눠서 한다.

사용법:
    python3 -m gonggu.llm_usage_report                     # 오늘 날짜
    DATE=2026-08-02 python3 -m gonggu.llm_usage_report     # 특정 날짜
    PRICE_INPUT_PER_1M=0.22 PRICE_OUTPUT_PER_1M=0.66 PRICE_CACHE_HIT_PER_1M=0.007 \\
        python3 -m gonggu.llm_usage_report                 # 단가 강제 지정(전 모델 공통)
"""
import datetime
import json
import os
from collections import defaultdict

from gonggu.common import LLM_USAGE_FILE

# 피크 시간대(UTC 기준, 끝 시각 제외). 공식 표기: "01:00 - 04:00 and 06:00 - 10:00 UTC
# (all other hours are off-peak)". 오프피크 단가는 피크의 정확히 절반이다.
# 한국시간(UTC+9)으로 옮기면 피크는 10:00~13:00, 15:00~19:00 KST — 데일리를 이 창 밖에서
# 돌리면 그대로 반값이다(실측: 2026-08-19 하루 $27.38 중 $8.02가 피크 구간에서 났다).
PEAK_HOURS_UTC = ((1, 4), (6, 10))

# 모델별 공식 단가($ / 1M tokens) — 2026-08-19 요금 개편 반영(공식 요금표 확인).
# 'input'은 캐시 미스 기준(청구 = miss분 × input + hit분 × cache_hit + 출력 × output).
# off는 peak의 정확히 절반이라 peak만 적고 코드에서 나눈다 — 두 벌을 손으로 적으면 한쪽만
# 고치는 드리프트가 생긴다.
#
# ⚠ 이전 값(2026-08-06 기준 input 0.14 / output 0.28 / cache_hit 0.0028)은 개편 전 단가였다.
# 그대로 뒀다면 실제 비용의 40%만 보여줬을 것이다(실측 2026-08-19: 보고 $10.74 vs 실제 $27.38).
MODEL_PRICES_PEAK = {
    'deepseek-v4-flash': {'input': 0.44, 'output': 1.32, 'cache_hit': 0.014},
    'deepseek-v4-pro': {'input': 1.32, 'output': 3.96, 'cache_hit': 0.044},
}


def is_peak(ts):
    """이 호출이 피크 시간대에 났는지. ts는 llm_usage.jsonl의 ISO 문자열.

    ⚠ 옛 기록(2026-08-19 이전)은 타임존 없는 로컬 시각이다 — naive면 이 머신의 로컬 타임존으로
    보고 UTC로 옮긴다(그 기록들이 실제로 로컬 시각이므로 맞다). 새 기록은 오프셋이 붙어 있어
    그대로 정확히 변환된다(common._log_usage 참고)."""
    try:
        dt = datetime.datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return False          # 시각을 못 읽으면 오프피크(싼 쪽)로 — 비용을 부풀리지 않는다
    if dt.tzinfo is None:
        dt = dt.astimezone()  # naive -> 로컬 타임존 부여
    hour = dt.astimezone(datetime.timezone.utc).hour
    return any(lo <= hour < hi for lo, hi in PEAK_HOURS_UTC)


def price_for(model, peak):
    """이 모델·시간대에 적용할 단가 dict 또는 None. 환경변수가 있으면 전 모델 공통으로
    우선 적용한다(이 경우 피크/오프피크 구분 없음 — 사용자가 준 값 하나를 그대로 쓴다)."""
    env_in, env_out = os.environ.get('PRICE_INPUT_PER_1M'), os.environ.get('PRICE_OUTPUT_PER_1M')
    if env_in and env_out:
        return {'input': float(env_in), 'output': float(env_out),
                'cache_hit': float(os.environ.get('PRICE_CACHE_HIT_PER_1M', '0'))}
    p = MODEL_PRICES_PEAK.get(model)
    if not p:
        return None
    return p if peak else {k: v / 2 for k, v in p.items()}   # 오프피크 = 피크의 절반


def cost_usd(totals, prices):
    """토큰 합계 × 단가 → 달러. 캐시 미스 토큰은 로그에 있으면 그 값을 쓰고(정확),
    없는 옛 기록만 prompt-hit으로 유도한다."""
    miss = totals.get('cache_miss')
    if not miss:
        miss = totals['prompt'] - totals['cache_hit']
    return (miss * prices['input']
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

    # (모델, 피크여부)로 나눠 담는다 — 단가가 시간대로 2배 갈리므로 합쳐놓고 곱하면 틀린다.
    def _zero():
        return {'calls': 0, 'prompt': 0, 'completion': 0, 'total': 0, 'cache_hit': 0,
                'cache_miss': 0}
    by_slot = defaultdict(_zero)
    by_model = defaultdict(_zero)
    for r in entries:
        model = r.get('model') or '?'
        peak = is_peak(r.get('ts'))
        for bucket in (by_slot[(model, peak)], by_model[model]):
            bucket['calls'] += 1
            bucket['prompt'] += r.get('prompt_tokens') or 0
            bucket['completion'] += r.get('completion_tokens') or 0
            bucket['total'] += r.get('total_tokens') or 0
            bucket['cache_hit'] += r.get('cache_hit_tokens') or 0
            bucket['cache_miss'] += r.get('cache_miss_tokens') or 0

    print(f'=== {date_str} LLM 사용량 (호출 {len(entries)}건) ===')
    grand_tokens, grand_cost, unknown_models = 0, 0.0, []
    peak_cost = off_cost = 0.0
    for model, m in sorted(by_model.items()):
        grand_tokens += m['total']
        hit_pct = 100 * m['cache_hit'] / m['prompt'] if m['prompt'] else 0
        line = (f"{model}: 호출 {m['calls']:,}건, 총 {m['total']:,} 토큰 "
                f"(입력 {m['prompt']:,} / 출력 {m['completion']:,}"
                + (f" / 캐시히트 {m['cache_hit']:,} = {hit_pct:.0f}%" if m['cache_hit'] else '') + ')')
        model_cost, priced = 0.0, False
        for peak in (True, False):
            slot = by_slot.get((model, peak))
            if not slot:
                continue
            prices = price_for(model, peak)
            if not prices:
                continue
            c = cost_usd(slot, prices)
            model_cost += c
            priced = True
            if peak:
                peak_cost += c
            else:
                off_cost += c
        if priced:
            grand_cost += model_cost
            line += f' -> ${model_cost:.4f}'
        else:
            unknown_models.append(model)
        print(' ', line)

    # 피크 구간이 실제로 얼마를 먹었는지 — 데일리 실행 시각을 옮길 근거가 되는 숫자다.
    if peak_cost:
        peak_calls = sum(v['calls'] for (_model, p), v in by_slot.items() if p)
        print(f'  ㄴ 피크 {peak_calls:,}건 ${peak_cost:.4f} / 오프피크 '
              f'{len(entries) - peak_calls:,}건 ${off_cost:.4f} '
              f'— 피크분을 오프피크로 옮기면 ${peak_cost / 2:.4f} 절약 '
              f'(피크: 10~13시·15~19시 KST)')

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
