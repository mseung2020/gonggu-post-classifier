#!/usr/bin/env python3
"""2단계: 01_raw의 각 포스트를 LLM#1(공구 분류)에 태워 공구 여부/상품명 배열/날짜/링크위치를
뽑는다. 체크포인트 저장이라 중간에 죽어도 이어서 실행 가능.

재시도/스레드풀/진행 로그 배관은 llm_batch.py 공용 러너를 쓴다(2단계 B2) — 이 파일에는
"무엇을 분류하는가"(프롬프트, 키, 저장 위치)만 남는다.

사용법:
    CONCURRENCY=4 python3 -m gonggu.classify            # 남은 것 전부
    LIMIT=500 python3 -m gonggu.classify                # 이번 실행에 500건만 (체크포인트 이어서)
    PLATFORM=yt LIMIT=500 python3 -m gonggu.classify    # ig/yt 중 하나만 골라서 500건
결과: data/02_classified/<발행일>.jsonl (원본 포스트 + classification 필드 추가, 날짜별,
    레코드 1개=1줄)
"""
import os
import sys

from gonggu.common import CLASSIFIED_DIR, DEEPSEEK_KEY, RAW_DIR, append_jsonl, call_llm, load_json_dir, post_date_key
from gonggu.llm_batch import retry_llm, run_llm_batch
from gonggu.prompts import GONGGU_CLASSIFY_SYSTEM, build_gonggu_classify_user


def _key(post):
    native_id = post.get('post_id') if post['platform'] == 'ig' else post.get('video_id')
    return f"{post['platform']}:{native_id}"


def classify_one(post):
    pub_date = post.get('publish_date') if post['platform'] == 'ig' else post.get('publishDate')
    user_message = build_gonggu_classify_user(
        description=post.get('description') or '',
        publish_date=pub_date or '',
        creator_description=post.get('creator_description') or '',
    )
    parsed, err = retry_llm(lambda: call_llm(GONGGU_CLASSIFY_SYSTEM, user_message))
    return {**post, 'classification': parsed, 'classification_error': err}


def main():
    if not DEEPSEEK_KEY:
        print('DEEPSEEK_KEY 환경변수가 없음 — .env에 채워넣을 것', file=sys.stderr)
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

    # 결과 1건 = 그 날짜 파일 끝에 한 줄 추가(append)만 한다 — 건수가 쌓여도 저장 비용이
    # 늘지 않는다(2026-07-27 실측/전환, 자세한 사연은 common.append_jsonl 참고).
    counters = run_llm_batch(
        todo, classify_one,
        lambda r: append_jsonl(CLASSIFIED_DIR / f'{post_date_key(r)}.jsonl', r),
        concurrency=concurrency, ok_start=len(done))

    total_done = len(done) + counters['ok'] + counters['err']
    print(f'총 {total_done}건(성공 {len(done) + counters["ok"]} / 실패 {counters["err"]}) '
          f'-> {CLASSIFIED_DIR}/*.jsonl (날짜별)')


if __name__ == '__main__':
    main()
