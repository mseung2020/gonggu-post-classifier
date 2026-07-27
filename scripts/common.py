"""파이프라인 전체가 공유하는 설정/DB 연결/Dify 호출 헬퍼."""
import json
import os
import pathlib

import pymysql
import requests
from dotenv import load_dotenv

ROOT = pathlib.Path(__file__).resolve().parent.parent
load_dotenv(ROOT / '.env')

# 각 단계 산출물을 발행일(YYYY-MM-DD.json)별로 쪼개서 폴더에 저장한다 — 폴더 이름 자체가
# "이 파일이 몇 번째 단계에서 나왔는지"를 보여주고, 날짜 파일 하나만 열어도 그날 무슨 포스트가
# 어느 단계까지 갔는지 바로 확인할 수 있다. resolve_links 내부의 상품 단위 체크포인트
# (link_resolution.json)는 날짜 필드가 없는 key-value 저장이라 그대로 단일 파일로 둔다.
RAW_DIR = ROOT / 'data/01_raw'
CLASSIFIED_DIR = ROOT / 'data/02_classified'
LOAD_READY_DIR = ROOT / 'data/03_load_ready'
RESOLVED_DIR = ROOT / 'data/04_resolved'

DIFY_URL = os.environ.get('DIFY_URL', 'https://api.dify.ai/v1').rstrip('/')
DIFY_KEY = os.environ.get('DIFY_KEY', '')

# classify.py/resolve_links가 스레드풀로 동시에 call_dify를 부르므로, 매 호출마다 새
# TCP/TLS 커넥션을 맺지 않도록 세션을 공유한다(requests.Session은 스레드 간 공유 안전 —
# 내부 urllib3 커넥션 풀이 스레드 세이프). pool_maxsize는 실측한 최대 동시성(약 48)보다
# 넉넉하게 잡아 풀 부족으로 인한 새 연결 생성을 막는다.
_session = requests.Session()
_adapter = requests.adapters.HTTPAdapter(pool_connections=64, pool_maxsize=64)
_session.mount('https://', _adapter)
_session.mount('http://', _adapter)

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
    r = _session.post(f'{DIFY_URL}/workflows/run', headers=headers, data=json.dumps(payload), timeout=timeout)
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


def dump_json(path, obj, indent=2):
    """임시 파일에 쓰고 os.replace로 교체 — 저장 도중 강제 종료돼도 기존 체크포인트 파일이
    반쯤 쓰인 상태로 깨지지 않는다(os.replace는 원자적). indent=None으로 부르면 pretty-print를
    건너뛰어 매번 전체를 다시 쓰는 대용량 체크포인트(예: classify.py)의 저장 비용을 줄인다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, indent=indent)
    os.replace(tmp, path)


def _date_key_from_raw(raw):
    """YYYY-MM-DD로 정규화. 날짜가 없거나 파싱 안 되면 '_unknown' 버킷으로 몰아서
    유실 없이(어느 파일을 봐야 할지도 바로 보이게) 남긴다."""
    s = str(raw)[:10] if raw else ''
    try:
        y, m, d = map(int, s.split('-'))
        assert 1 <= m <= 12 and 1 <= d <= 31
    except Exception:
        return '_unknown'
    return s


def post_date_key(post):
    """raw/classified 단계 레코드(최상위에 platform+publish_date/publishDate가 있는 형태)의
    발행일 버킷 키."""
    raw = post.get('publish_date') if post.get('platform') == 'ig' else post.get('publishDate')
    return _date_key_from_raw(raw)


def parent_date_key(item):
    """transform 이후 단계 레코드({platform, parent, products} 형태)의 발행일 버킷 키."""
    parent = item.get('parent') or {}
    raw = parent.get('publish_date') if item.get('platform') == 'ig' else parent.get('publishDate')
    return _date_key_from_raw(raw)


def load_json_dir(dir_path):
    """dir_path 밑의 날짜별 *.json을 파일명(=날짜) 순으로 이어붙여 하나의 리스트로 반환."""
    if not dir_path.exists():
        return []
    out = []
    for f in sorted(dir_path.glob('*.json')):
        out.extend(load_json(f))
    return out


def dump_json_sharded(dir_path, records, date_fn, only_keys=None):
    """records를 date_fn(record) 기준으로 묶어 dir_path/<날짜>.json에 저장한다. only_keys를
    주면 그 날짜들만 다시 쓴다 — 매번 records 전체를 각 파일에 다시 쓰면 날짜별로 쪼갠
    보람이 없어지므로, 이번에 바뀐 날짜만 갱신하는 용도(classify.py의 체크포인트처럼)."""
    buckets = {}
    for r in records:
        buckets.setdefault(date_fn(r), []).append(r)
    for k in (only_keys if only_keys is not None else buckets.keys()):
        dump_json(dir_path / f'{k}.json', buckets.get(k, []), indent=None)


def clear_json_dir(dir_path):
    """매 실행마다 전체를 처음부터 다시 계산하는 단계(transform.py, resolve_links의 최종
    산출물)에서 쓴다 — 재계산 결과 특정 날짜에 해당하는 레코드가 하나도 안 남으면, 그 날짜의
    옛 파일이 갱신되지 않고 그대로 남아 stale 데이터가 되는 걸 막기 위해 먼저 비운다."""
    if dir_path.exists():
        for f in dir_path.glob('*.json'):
            f.unlink()
