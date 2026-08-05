#!/usr/bin/env python3
"""신규 모듈(기존 파이프라인과 완전 독립) 2/2 — fetch_yt_ppl.py가 저장한
data/01_raw_yt_ppl/의 각 영상을 이 파일 전용 프롬프트(prompts.YT_PPL_GONGGU_SYSTEM)로
"PPL이지만 사실상 그룹특가인지" 판별한다.

classify.py(LLM#1, GONGGU_CLASSIFY_SYSTEM)는 이 목적으로 재사용하지 않는다 — 이미 검증된
그 판정 로직/파일에 전혀 손대지 않기 위해 fetch도, 프롬프트도, 스크립트도 완전히 분리했다.
결과는 classify.py와 같은 data/02_classified/<발행일>.jsonl에 같은 레코드 스키마로
append하므로 transform.py부터는 무수정으로 이 결과를 그대로 처리한다. 체크포인트 저장이라
중간에 죽어도 이어서 실행 가능. 재시도/스레드풀/진행 로그 배관은 llm_batch.py 공용
러너(2단계 B2)를 쓴다.

사용법:
    CONCURRENCY=100 python3 -m gonggu.classify_yt_ppl
    LIMIT=20 python3 -m gonggu.classify_yt_ppl          # 소량 스모크 테스트
결과: data/02_classified/<발행일>.jsonl (classify.py와 동일 디렉터리를 공유하지만 서로 다른
    video_id만 다루므로 충돌 없이 append됨)
"""
import os
import sys

from gonggu.common import CLASSIFIED_DIR, DEEPSEEK_KEY, ROOT, append_jsonl, call_llm, load_json_dir, post_date_key
from gonggu.llm_batch import retry_llm, run_llm_batch
from gonggu.prompts import YT_PPL_GONGGU_SYSTEM, build_yt_ppl_gonggu_user

RAW_DIR_YT_PPL = ROOT / 'data/01_raw_yt_ppl'


def _key(post):
    return f"yt:{post['video_id']}"


def classify_one(post):
    user_message = build_yt_ppl_gonggu_user(
        title=post.get('title') or '',
        description=post.get('description') or '',
        brand_name=post.get('brand_name'),
        sponsored_type=post.get('sponsored_type'),
    )
    parsed, err = retry_llm(lambda: call_llm(YT_PPL_GONGGU_SYSTEM, user_message))
    return {**post, 'classification': parsed, 'classification_error': err}


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

    counters = run_llm_batch(
        todo, classify_one,
        lambda r: append_jsonl(CLASSIFIED_DIR / f'{post_date_key(r)}.jsonl', r),
        concurrency=concurrency,
        extra=('공구판정', lambda r: bool((r.get('classification') or {}).get('is_gonggu'))))

    total_done = already_done + counters['ok'] + counters['err']
    print(f'총 {total_done}건(성공 {counters["ok"]} / 실패 {counters["err"]} / 공구판정 {counters["extra"]}) '
          f'-> {CLASSIFIED_DIR}/*.jsonl (날짜별, classify.py와 공유)')


if __name__ == '__main__':
    main()
