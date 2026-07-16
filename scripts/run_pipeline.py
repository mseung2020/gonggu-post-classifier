#!/usr/bin/env python3
"""전체 파이프라인을 한 번에 실행: fetch_source → classify → transform → resolve_links → load.

resolve_links.py는 Playwright로 실제 크롤링을 하는 느린 단계라서(안티봇 회피 대기 포함, 상품당
수 초) DIFY_KEY_PICK/DIFY_KEY_JUDGE가 아직 없거나 이번엔 건너뛰고 싶으면 --skip-resolve로
뺄 수 있다 — 이 경우 load.py는 transform.py가 만든 candidate_url(LLM 원본 후보, 세미콜론
이어붙임)을 그대로 쓴다.

사용법:
    python3 scripts/run_pipeline.py                 # 5단계 전부
    python3 scripts/run_pipeline.py --skip-resolve   # 링크 해석 건너뛰고 원본 후보로 바로 load
    python3 scripts/run_pipeline.py --skip-load      # DB에 안 넣고 확인만(load_ready.json까지)
    DAYS_BACK=14 python3 scripts/run_pipeline.py     # 최근 14일치
"""
import subprocess
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent


def run(script):
    print(f'\n=== {script} ===')
    result = subprocess.run([sys.executable, str(ROOT / script)])
    if result.returncode != 0:
        print(f'{script} 실패 (exit {result.returncode}) — 파이프라인 중단', file=sys.stderr)
        sys.exit(result.returncode)


def main():
    skip_load = '--skip-load' in sys.argv
    skip_resolve = '--skip-resolve' in sys.argv
    run('fetch_source.py')
    run('classify.py')
    run('transform.py')
    if skip_load:
        print('\n--skip-load 지정됨 — data/output/load_ready.json 확인 후 python3 scripts/load.py로 직접 실행할 것')
        return
    if not skip_resolve:
        run('resolve_links.py')
    run('load.py')
    print('\n파이프라인 완료.')


if __name__ == '__main__':
    main()
