#!/usr/bin/env python3
"""전체 파이프라인을 한 번에 실행: fetch_source → classify → transform → load.

사용법:
    python3 scripts/run_pipeline.py                 # 4단계 전부
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
    run('fetch_source.py')
    run('classify.py')
    run('transform.py')
    if skip_load:
        print('\n--skip-load 지정됨 — data/output/load_ready.json 확인 후 python3 scripts/load.py로 직접 실행할 것')
        return
    run('load.py')
    print('\n파이프라인 완료.')


if __name__ == '__main__':
    main()
