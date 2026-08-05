#!/usr/bin/env python3
"""전체 파이프라인을 스테이지 순서로 한 번씩 실행: fetch_source → classify → transform →
resolve_links → load. 각 스테이지는 그 시점에 남아 있는 데이터 전체를 한 번에 처리하고
완전히 끝난 뒤에야 다음 스테이지로 넘어간다(청크 단위로 여러 스테이지를 번갈아 도는 방식이
아님) — classify.py/resolve_links 자체가 내부적으로 동시성(CONCURRENCY/
RESOLVE_CONCURRENCY)과 체크포인트를 갖고 있으므로 오케스트레이션은 순서 보장에만 집중한다.

resolve_links는 Playwright로 실제 크롤링을 하는 느린 단계라서(안티봇 회피 대기 포함, 상품당
수 초) DEEPSEEK_KEY가 아직 없거나 이번엔 건너뛰고 싶으면 --skip-resolve로
뺄 수 있다 — 이 경우 load.py는 transform.py가 만든 candidate_url(LLM 원본 후보, 세미콜론
이어붙임)을 그대로 쓴다.

Ctrl+C로 언제든 중단해도 안전하다 — classify.py/resolve_links는 체크포인트를 저장하고,
transform.py/load.py는 이미 처리·삽입된 건 자동으로 건너뛰므로, 같은 명령을 다시 실행하면
멈췄던 지점부터 이어서 진행된다.

사용법:
    python3 scripts/run_pipeline.py                 # fetch부터 load까지 5단계 전부
    FETCH_FIRST=1 python3 scripts/run_pipeline.py    # 원본을 새로 가져오는 것부터 시작
    DAYS_BACK=14 FETCH_FIRST=1 python3 scripts/run_pipeline.py   # 최근 14일치로 새로 가져오기
    python3 scripts/run_pipeline.py --skip-resolve   # 링크 해석 건너뛰고 원본 후보로 바로 load
    python3 scripts/run_pipeline.py --skip-load      # DB에 안 넣고 확인만(load_ready.json까지)

모듈별로 따로 실행하고 싶으면(중간 결과를 직접 확인하며 진행) README의 "모듈별로 따로 실행"
절 참고 — 이 스크립트는 그 단계들을 정해진 순서로 이어 부르기만 할 뿐, 각 스크립트가 하는 일
자체는 바꾸지 않는다.
"""
import os
import pathlib
import subprocess
import sys

from gonggu.common import CLASSIFIED_DIR, RAW_DIR, connect_dst, load_json_dir

# 패키지화 이후(2026-08-05) 모든 단계는 저장소 루트에서 `python3 -m gonggu.<모듈>`로 실행한다 —
# 예전처럼 scripts/ 경로나 실행 디렉터리에 따라 임포트가 갈라지는 문제가 없다.
REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
TARGET_TABLES = ('gonggu_post', 'gonggu_post_product', 'gonggu_video', 'gonggu_video_product')


def run(module, args=()):
    label = f'-m gonggu.{module}'
    print(f'\n=== {label} ===')
    result = subprocess.run([sys.executable, '-m', f'gonggu.{module}', *args], cwd=REPO_ROOT)
    if result.returncode != 0:
        print(f'{label} 실패 (exit {result.returncode}) — 파이프라인 중단', file=sys.stderr)
        sys.exit(result.returncode)


def _key(r):
    native_id = r.get('post_id') if r['platform'] == 'ig' else r.get('video_id')
    return f"{r['platform']}:{native_id}"


def print_remaining():
    if not RAW_DIR.exists():
        return
    posts = load_json_dir(RAW_DIR)
    done = load_json_dir(CLASSIFIED_DIR)
    # classification_error가 남은 건 완료로 안 치는 classify.py 판정과 맞춘다(재시도 대상).
    done_keys = {_key(r) for r in done if r.get('classification') and not r.get('classification_error')}
    remaining = {'ig': 0, 'yt': 0}
    for p in posts:
        if _key(p) not in done_keys:
            remaining[p['platform']] += 1
    print(f'  분류 대상 남음 — ig {remaining["ig"]} / yt {remaining["yt"]}')


def print_db_summary():
    conn = connect_dst()
    try:
        with conn.cursor() as cur:
            counts = {}
            for t in TARGET_TABLES:
                cur.execute('SELECT COUNT(*) AS n FROM ' + t)
                counts[t] = cur.fetchone()['n']
    finally:
        conn.close()
    print('\n[dev_gongguking 현재 누적 행 수]')
    for t, n in counts.items():
        print(f'  {t}: {n}')


def main():
    skip_load = '--skip-load' in sys.argv
    skip_resolve = '--skip-resolve' in sys.argv

    if os.environ.get('FETCH_FIRST') == '1':
        run('fetch_source')

    print_remaining()
    run('classify')  # 이 시점의 원본 전체(ig+yt)를 한 번에 — 내부 CONCURRENCY로 동시 처리

    run('transform')  # 02_classified 전체를 다시 게이트링(항상 전체 재계산, 결정론적)

    if skip_load:
        print('\n--skip-load 지정됨 — data/03_load_ready/ 확인 후 python3 -m gonggu.load로 직접 실행할 것')
        return

    if not skip_resolve:
        run('resolve_links')  # 03_load_ready 전체 중 아직 해석 안 된 상품만

    run('load')
    print('\n파이프라인 완료.')
    print_db_summary()


if __name__ == '__main__':
    main()
