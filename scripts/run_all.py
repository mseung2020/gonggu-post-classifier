#!/usr/bin/env python3
"""posts_raw.json에 있는 전체 포스트를 CHUNK_SIZE(기본 100)씩 끊어서 인스타/유튜브를
번갈아 classify → transform → load까지 자동으로 반복한다. 한쪽 플랫폼이 다 끝나면
자동으로 감지해서 남은 플랫폼만 계속 진행하고, 둘 다 끝나면 자동 종료한다.

Ctrl+C로 언제든 중단해도 안전하다 — classify.py가 10건마다 체크포인트를 저장하고,
transform.py/load.py는 이미 처리·삽입된 건 자동으로 건너뛰기 때문에 다시 실행하면
그대로 이어서 진행된다.

사용법:
    python3 scripts/run_all.py                    # fetch는 이미 했다는 전제, 100개씩 반복
    CHUNK_SIZE=200 python3 scripts/run_all.py      # 200개씩
    FETCH_FIRST=1 DAYS_BACK=7 python3 scripts/run_all.py   # fetch부터 새로 시작
"""
import os
import pathlib
import subprocess
import sys

from common import CLASSIFIED_FILE, RAW_FILE, connect_dst, load_json

ROOT_SCRIPTS = pathlib.Path(__file__).resolve().parent
CHUNK_SIZE = int(os.environ.get('CHUNK_SIZE', '100'))
CONCURRENCY = os.environ.get('CONCURRENCY', '6')


def run(script, env_extra=None):
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    result = subprocess.run([sys.executable, str(ROOT_SCRIPTS / script)], env=env)
    if result.returncode != 0:
        print(f'{script} 실패 (exit {result.returncode}) — 파이프라인 중단', file=sys.stderr)
        sys.exit(result.returncode)


def _key(r):
    native_id = r.get('post_id') if r['platform'] == 'ig' else r.get('video_id')
    return f"{r['platform']}:{native_id}"


def remaining_counts():
    posts = load_json(RAW_FILE)
    done = load_json(CLASSIFIED_FILE) if CLASSIFIED_FILE.exists() else []
    done_keys = {_key(r) for r in done}
    remaining = {'ig': 0, 'yt': 0}
    for p in posts:
        if _key(p) not in done_keys:
            remaining[p['platform']] += 1
    return remaining


def print_db_summary():
    conn = connect_dst()
    try:
        with conn.cursor() as cur:
            counts = {}
            for t in ('gonggu_post', 'gonggu_post_product', 'gonggu_video', 'gonggu_video_product'):
                cur.execute(f'SELECT COUNT(*) AS n FROM {t}')
                counts[t] = cur.fetchone()['n']
    finally:
        conn.close()
    print('\n[dev_gongguking 현재 누적 행 수]')
    for t, n in counts.items():
        print(f'  {t}: {n}')


def main():
    if os.environ.get('FETCH_FIRST') == '1':
        print('=== fetch_source.py (원본 다시 가져오기) ===')
        run('fetch_source.py')

    round_num = 0
    while True:
        remaining = remaining_counts()
        total = remaining['ig'] + remaining['yt']
        if total == 0:
            print('\n모든 포스트 분류 완료. 더 처리할 게 없습니다.')
            break

        round_num += 1
        print(f'\n===== 라운드 {round_num} | 남음: ig {remaining["ig"]} / yt {remaining["yt"]} =====')

        for platform in ('ig', 'yt'):
            if remaining[platform] == 0:
                continue
            n = min(CHUNK_SIZE, remaining[platform])
            print(f'\n--- {platform.upper()} {n}건 분류 ---')
            run('classify.py', {'PLATFORM': platform, 'LIMIT': str(CHUNK_SIZE), 'CONCURRENCY': CONCURRENCY})
            print(f'--- {platform.upper()} 배치 → DB 반영 ---')
            run('transform.py')
            run('load.py')

    print_db_summary()


if __name__ == '__main__':
    main()
