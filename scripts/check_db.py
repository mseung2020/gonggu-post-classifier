#!/usr/bin/env python3
"""소스/타겟 DB 연결과 타겟 테이블 스키마를 확인하는 1회성 점검 스크립트."""
import os
import pathlib

import pymysql
from dotenv import load_dotenv

ROOT = pathlib.Path(__file__).resolve().parent.parent
load_dotenv(ROOT / '.env')


def connect(prefix):
    return pymysql.connect(
        host=os.environ[f'{prefix}_DB_HOST'],
        port=int(os.environ.get(f'{prefix}_DB_PORT', 3306)),
        user=os.environ[f'{prefix}_DB_USER'],
        password=os.environ[f'{prefix}_DB_PASSWORD'],
        database=os.environ[f'{prefix}_DB_NAME'],
        connect_timeout=10,
    )


def main():
    print('--- SRC (hifen) 연결 확인 ---')
    src = connect('SRC')
    with src.cursor() as cur:
        cur.execute('SELECT COUNT(*) FROM instagram_post')
        print('instagram_post 행 수:', cur.fetchone()[0])
    src.close()
    print('SRC OK')

    print('--- DST (dev_gongguking) 연결 확인 ---')
    dst = connect('DST')
    with dst.cursor() as cur:
        cur.execute('SHOW TABLES LIKE "gonggu_%"')
        tables = [r[0] for r in cur.fetchall()]
        print('gonggu_* 테이블:', tables)
        for t in tables:
            cur.execute(f'DESCRIBE {t}')
            cols = [r[0] for r in cur.fetchall()]
            print(f'  {t} 컬럼:', cols)
    dst.close()
    print('DST OK')


if __name__ == '__main__':
    main()
