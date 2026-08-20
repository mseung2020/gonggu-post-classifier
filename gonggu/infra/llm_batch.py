"""여러 레코드를 LLM에 동시 다발로 태우는 공통 배관(대공사 2단계 B2, 2026-08-05).

classify.py / classify_yt_ppl.py / classify_category.py 세 파일에 거의 같은 코드가 3벌
복제되어 있던 것(재시도 루프, 스레드풀 + 진행 로그, 실패 샘플 출력)을 여기로 모았다 —
한쪽에만 버그 수정이 반영되는 사고(backfill_period의 LazyPage 누락 같은)를 이 계열에서
구조적으로 막기 위함. 각 스크립트에는 "무엇을 분류하는가"(프롬프트/키/저장 위치)만 남는다.

정책 값(MAX_RETRY 등)과 재시도 동작은 기존 세 파일의 것과 완전히 동일하다.
"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

MAX_RETRY = 3
# 429(레이트리밋)는 코드 버그가 아니라 "잠깐 기다리면 반드시 풀리는" 상태라 훨씬 길게/많이
# 재시도한다 — 짧게 3번만 시도하고 포기하면 대량 동시 처리 시 전부 영구 실패로 남는다.
MAX_RETRY_429 = 10


def is_429(e):
    return isinstance(e, requests.exceptions.HTTPError) and e.response is not None and e.response.status_code == 429


def retry_llm(call):
    """call()(call_llm을 감싼 무인자 함수)을 재시도 정책과 함께 실행한다.
    반환: (파싱된 결과, None) 또는 (None, 마지막 에러 문자열[:200]).

    - 429: 최대 MAX_RETRY_429회, 대기 min(60, 5×시도횟수)초 — 기다리면 풀리는 상태.
    - 그 외 예외: 최대 MAX_RETRY회, 대기 1.5×시도횟수초."""
    last_err = None
    rate_limit_attempt = 0
    generic_attempt = 0
    while True:
        try:
            return call(), None
        except Exception as e:
            last_err = str(e)[:200]
            if is_429(e):
                rate_limit_attempt += 1
                if rate_limit_attempt > MAX_RETRY_429:
                    return None, last_err
                time.sleep(min(60, 5 * rate_limit_attempt))
                continue
            generic_attempt += 1
            if generic_attempt >= MAX_RETRY:
                return None, last_err
            time.sleep(1.5 * generic_attempt)


def run_llm_batch(todo, process_one, persist_one, *, concurrency,
                  error_of=lambda r: r.get('classification_error'),
                  ok_start=0, extra=None, report_every=30):
    """todo의 각 항목을 스레드풀에서 process_one으로 처리하고, 결과가 나오는 즉시 lock 안에서
    persist_one(결과)로 저장(append)한 뒤 진행 상황을 출력한다.

    - process_one(item) -> result  : 스레드에서 실행(LLM 호출 포함). 예외를 밖으로 던지지 말고
      결과 레코드의 에러 필드에 담아 반환할 것(retry_llm 사용 권장 — 기존 세 스크립트와 동일).
    - persist_one(result)          : lock 안에서 호출됨(append_jsonl 등 저장 1건).
    - error_of(result) -> str|None : 이 결과가 실패인지 판정(실패 샘플 출력/집계에 사용).
    - ok_start                     : "누적 성공"의 시작값(이미 완료된 체크포인트 건수) —
                                     classify.py가 누적을 완료분 포함으로 세던 것을 유지.
    - extra=(라벨, predicate)      : 성공 건 중 predicate(result)가 참인 것을 별도로 세어
                                     진행 로그에 " / 라벨 n"으로 덧붙임(classify_yt_ppl의 공구판정).

    반환: {'ok': 이번 실행 성공, 'err': 이번 실행 실패, 'extra': extra 카운트}."""
    lock = threading.Lock()
    ok_total = ok_start
    err_total = 0
    extra_total = 0
    batch_ok = 0
    batch_err = 0
    batch_err_samples = []
    n = len(todo)

    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futures = {ex.submit(process_one, item): item for item in todo}
        for i, fut in enumerate(as_completed(futures), 1):
            r = fut.result()
            with lock:
                persist_one(r)
                if error_of(r):
                    err_total += 1
                    batch_err += 1
                    batch_err_samples.append(str(error_of(r))[:80])
                else:
                    ok_total += 1
                    batch_ok += 1
                    if extra and extra[1](r):
                        extra_total += 1
                if i % report_every == 0 or i == n:
                    extra_txt = f' / {extra[0]} {extra_total}' if extra else ''
                    print(f'  {i}/{n} 완료 — 이번 배치 성공 {batch_ok} / 실패 {batch_err} '
                          f'(누적 성공 {ok_total} / 실패 {err_total}{extra_txt})')
                    for s in batch_err_samples[:3]:  # 실패 사유를 바로 보여줘서 429 재발 같은 걸 빨리 눈치채게
                        print(f'    실패 예시: {s}')
                    batch_ok = batch_err = 0
                    batch_err_samples = []

    return {'ok': ok_total - ok_start, 'err': err_total, 'extra': extra_total}
