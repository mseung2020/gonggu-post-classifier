"""파이프라인 전체가 공유하는 설정/DB 연결/Dify 호출 헬퍼."""
import json
import os
import pathlib

import pymysql
import requests
from dotenv import load_dotenv

ROOT = pathlib.Path(__file__).resolve().parent.parent
load_dotenv(ROOT / '.env')

RAW_FILE = ROOT / 'data/raw/posts_raw.json'
CLASSIFIED_FILE = ROOT / 'data/output/classified.json'
LOAD_READY_FILE = ROOT / 'data/output/load_ready.json'
RESOLVED_FILE = ROOT / 'data/output/load_ready_resolved.json'

DIFY_URL = os.environ.get('DIFY_URL', 'https://api.dify.ai/v1').rstrip('/')
DIFY_KEY = os.environ.get('DIFY_KEY', '')

# 쿠팡파트너스/네이버쇼핑커넥트 'TOP N 추천' 리뷰 — 법정 고지문구 매칭이라 규칙으로 유지
# (7월_co_buying_data/scripts/resolver.py의 AFFILIATE_MARKERS와 동일)
AFFILIATE_MARKERS = ('파트너스', '쇼핑커넥트', '일정액의 수수료', '수수료를 제공받습니다')


def _connect(prefix):
    return pymysql.connect(
        host=os.environ[f'{prefix}_DB_HOST'],
        port=int(os.environ.get(f'{prefix}_DB_PORT', 3306)),
        user=os.environ[f'{prefix}_DB_USER'],
        password=os.environ[f'{prefix}_DB_PASSWORD'],
        database=os.environ[f'{prefix}_DB_NAME'],
        charset='utf8mb4',
        connect_timeout=15,
        cursorclass=pymysql.cursors.DictCursor,
    )


def connect_src():
    """hifen — 원본 인스타/유튜브 데이터, 읽기 전용으로만 사용."""
    return _connect('SRC')


def connect_dst():
    """dev_gongguking — gonggu_post/gonggu_product에 쓰기."""
    return _connect('DST')


def call_dify(input_obj, api_key=None, timeout=60):
    headers = {'Authorization': f'Bearer {api_key or DIFY_KEY}', 'Content-Type': 'application/json'}
    payload = {'inputs': {'input': input_obj}, 'response_mode': 'blocking', 'user': 'gonggu-post-classifier'}
    r = requests.post(f'{DIFY_URL}/workflows/run', headers=headers, data=json.dumps(payload), timeout=timeout)
    r.raise_for_status()
    data = r.json()
    raw = (data.get('data', {}).get('outputs', {}) or {}).get('result', '')
    try:
        return json.loads(raw)
    except Exception:
        s, e = raw.find('{'), raw.rfind('}')
        if s != -1 and e != -1:
            return json.loads(raw[s:e + 1])
        raise ValueError(f'JSON 파싱 실패: {raw[:200]}')


def is_affiliate_ranking(description, urls):
    return len(urls or []) >= 3 and any(m in (description or '') for m in AFFILIATE_MARKERS)


def load_json(path):
    return json.load(open(path, encoding='utf-8'))


def dump_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    json.dump(obj, open(path, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
