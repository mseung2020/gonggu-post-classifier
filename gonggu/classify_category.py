#!/usr/bin/env python3
"""build_category_dataset.py가 만든 제품 목록(JSONL)을 LLM#4(카테고리 분류)에 태워
각 제품에 category/subcategory를 붙인다. 체크포인트 저장이라 중간에 죽어도 이어서 실행 가능.
입력 파일의 줄 번호(row_id)로 완료 여부를 추적하므로, 입력 파일을 다시 만들지 않는 한
안전하게 재실행할 수 있다. 재시도/스레드풀/진행 로그 배관은 llm_batch.py 공용 러너(2단계 B2).

사용법:
    python3 -m gonggu.classify_category                       # 기본 입출력 경로, 남은 것 전부
    LIMIT=20 python3 -m gonggu.classify_category               # 이번 실행에 20건만(체크포인트 이어서)
    CONCURRENCY=8 python3 -m gonggu.classify_category
    ESCALATION_THRESHOLD=0.7 python3 -m gonggu.classify_category    # 기본값도 0.7
    python3 -m gonggu.classify_category <입력.jsonl> <출력.jsonl>

2단 캐스케이드: 모든 제품을 먼저 DEEPSEEK_MODEL_FLASH(싼 모델)로 분류하고, confidence가
ESCALATION_THRESHOLD 이상이면 그 결과를 바로 최종으로 쓴다. 미만이면 같은 프롬프트로
DEEPSEEK_MODEL(프로, 더 비싼 모델)에 한 번 더 태워서 그 결과를 최종으로 쓴다(두 모델이 서로
다르니 프롬프트 캐시는 당연히 공유되지 않는다). category_taxonomy에 모든 대카테고리가
"기타" 하위카테고리를, 그리고 "기타" 자체도 16번째 대카테고리로 갖고 있어서 뭘 골라도
항상 유효한 값이 나온다 — 그래서 "미분류"로 강제로 빼는 로직은 없다(카테고리 체계
자체에 항상 마지막 안전망이 있음).

결과: <출력.jsonl> (입력 레코드 + category/subcategory/confidence/reason/classify_error 필드,
    레코드 1개=1줄). 추가로 decided_by("flash"|"pro")와 flash_category/flash_subcategory/
    flash_confidence(1차 스크리닝 결과, 참고용)도 같이 남는다. llm_category/llm_subcategory는
    최종 결정을 내린 단계의 원본(교정 전) 값이다.
"""
import json
import os
import pathlib
import sys

from gonggu.common import CATEGORY_TAXONOMY, DEEPSEEK_KEY, DEEPSEEK_MODEL, DEEPSEEK_MODEL_FLASH, \
    SUBCATEGORY_TO_CATEGORY, call_llm
from gonggu.llm_batch import retry_llm, run_llm_batch
from gonggu.prompts import CATEGORY_CLASSIFY_SYSTEM, build_category_classify_user

IN_DEFAULT = pathlib.Path.home() / 'Desktop' / 'gonggu_category_input.jsonl'
OUT_DEFAULT = pathlib.Path.home() / 'Desktop' / 'gonggu_category_result.jsonl'

ESCALATION_THRESHOLD = float(os.environ.get('ESCALATION_THRESHOLD', '0.7'))


def _load_jsonl(path):
    if not path.exists():
        return []
    with open(path, encoding='utf-8') as f:
        return [json.loads(line) for line in f if line.strip()]


def _call_stage(model, user_message):
    """한 모델로 한 번 호출 — 429/일시 오류는 그 호출 안에서만 재시도한다(캐스케이드에서
    플래시가 이미 성공했는데 프로 호출 실패로 플래시까지 다시 부르는 낭비를 막기 위해)."""
    return retry_llm(lambda: call_llm(CATEGORY_CLASSIFY_SYSTEM, user_message, model=model))


def _extract(parsed):
    """응답 JSON에서 category/subcategory/confidence/reason을 뽑고, LLM이 category 자리에
    subcategory 문자열을 잘못 넣는 경우(예: category="여행/캐리어")를 교정한다."""
    llm_category, llm_subcategory = parsed.get('category'), parsed.get('subcategory')
    category, subcategory = llm_category, llm_subcategory
    if category not in CATEGORY_TAXONOMY and category in SUBCATEGORY_TO_CATEGORY:
        subcategory = subcategory or category
        category = SUBCATEGORY_TO_CATEGORY[category]
    return {
        'category': category, 'subcategory': subcategory,
        'confidence': parsed.get('confidence'), 'reason': parsed.get('reason'),
        'llm_category': llm_category, 'llm_subcategory': llm_subcategory,
    }


def classify_one(row):
    user_message = build_category_classify_user(
        product_name=row.get('product_name') or '',
        title=row.get('title') or '',
        description=row.get('description') or '',
    )

    flash_parsed, err = _call_stage(DEEPSEEK_MODEL_FLASH, user_message)
    if flash_parsed is None:
        return {**row, 'category': None, 'subcategory': None, 'classify_error': err}
    flash = _extract(flash_parsed)

    if isinstance(flash['confidence'], (int, float)) and flash['confidence'] >= ESCALATION_THRESHOLD:
        final, decided_by = flash, 'flash'
    else:
        pro_parsed, err = _call_stage(DEEPSEEK_MODEL, user_message)
        if pro_parsed is None:
            return {**row, 'category': None, 'subcategory': None, 'classify_error': err,
                    'flash_category': flash['category'], 'flash_subcategory': flash['subcategory'],
                    'flash_confidence': flash['confidence']}
        final = _extract(pro_parsed)
        decided_by = 'pro'

    return {
        **row,
        'category': final['category'], 'subcategory': final['subcategory'],
        'confidence': final['confidence'], 'reason': final['reason'],
        'llm_category': final['llm_category'], 'llm_subcategory': final['llm_subcategory'],
        'decided_by': decided_by,
        'flash_category': flash['category'], 'flash_subcategory': flash['subcategory'],
        'flash_confidence': flash['confidence'],
        'classify_error': None,
    }


def main():
    if not DEEPSEEK_KEY:
        print('DEEPSEEK_KEY 환경변수가 없음 — .env에 채워넣을 것', file=sys.stderr)
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

    def _persist(r):
        with open(out_path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(r, ensure_ascii=False) + '\n')

    counters = run_llm_batch(todo, classify_one, _persist, concurrency=concurrency,
                             error_of=lambda r: r.get('classify_error'), ok_start=len(done))

    total_done = len(done) + counters['ok'] + counters['err']
    print(f'총 {total_done}건(성공 {len(done) + counters["ok"]} / 실패 {counters["err"]}) -> {out_path}')


if __name__ == '__main__':
    main()
