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

# dify_workflows/04_category_classify.yml 프롬프트에 박아넣은 것과 동일한 체계 — 여기가
# 바뀌면 그 yml도 같이 바꿔야 한다. classify_category.py/category_dashboard.py가 공유해서
# 쓴다(따로 들고 있으면 서로 어긋날 수 있어서 한 곳에 둠).
CATEGORY_TAXONOMY = {
    '뷰티': ['스킨케어(로션/크림)', '세럼/앰플', '클렌징', '선케어', '마스크팩', '메이크업', '헤어/바디', '향수', '뷰티기기'],
    '식품': ['반찬/밀키트', '정육/수산', '과일/농산물', '간식/디저트', '음료/커피', '냉동/간편식', '김치/장류', '곡물/견과'],
    '영양제': ['비타민/종합', '유산균', '홍삼', '오메가3', '콜라겐', '관절/눈/간'],
    '다이어트/헬스': ['프로틴/쉐이크', '다이어트보조제', '식단관리식품', '홈트/헬스기구', '운동보조용품', '바디관리기기', '운동복/슬리밍웨어'],
    '육아': ['분유/이유식', '기저귀/물티슈', '유아동의류', '유아용품', '완구/교육', '유모차/카시트', '임산부', '유아동도서'],
    '패션': ['상의', '하의/스커트', '원피스', '아우터', '이너/속옷', '홈웨어/잠옷', '빅사이즈/임부복'],
    '신발/가방/주얼리': ['운동화', '구두/부츠', '슬리퍼/샌들', '여성가방', '남성가방/백팩', '주얼리', '시계', '모자/벨트'],
    '살림/청소': ['수납/정리', '세탁/세제', '청소용품', '욕실', '방향/탈취·제습', '휴지/생활소모품'],
    '주방': ['냄비/프라이팬', '밀폐용기', '칼/도마', '식기/컵', '텀블러/물병', '주방가전', '베이킹'],
    '가전/디지털': ['생활가전', '계절가전', '음향기기', '모바일 액세서리', 'PC/주변기기', '프린터'],
    '인테리어': ['침구/패브릭', '커튼/블라인드', '러그', '조명', '가구', '홈데코', '디퓨저/캔들'],
    '반려동물': ['사료/간식', '배변용품', '장난감', '미용/위생', '하네스/이동장', '영양제', '의류'],
    '스포츠/취미': ['등산/트레킹', '자전거/라이딩', '골프', '구기/수영', '캠핑/차박', '여행/캐리어', '문구/도서', '자동차용품'],
}

# LLM이 category 필드에 subcategory 문자열을 잘못 넣는 경우(예: category="여행/캐리어")를
# 자동으로 바로잡기 위한 역방향 조회 테이블. 이 taxonomy 안에서 하위카테고리 문자열은
# 카테고리 간에 겹치지 않으므로(직접 대조 확인함) 되돌릴 곳이 항상 유일하게 정해진다.
SUBCATEGORY_TO_CATEGORY = {
    sub: cat for cat, subs in CATEGORY_TAXONOMY.items() for sub in subs
}

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


def call_dify(input_obj, api_key=None, timeout=60, raw_inputs=False):
    """raw_inputs=True면 input_obj를 그대로 'inputs'로 보낸다(워크플로우 Start 노드에 변수가
    여러 개 있는 경우). 기본값(False)은 기존 워크플로우들처럼 단일 'input' json_object 변수로 감싼다."""
    headers = {'Authorization': f'Bearer {api_key or DIFY_KEY}', 'Content-Type': 'application/json'}
    inputs = input_obj if raw_inputs else {'input': input_obj}
    payload = {'inputs': inputs, 'response_mode': 'blocking', 'user': 'gonggu-post-classifier'}
    r = _session.post(f'{DIFY_URL}/workflows/run', headers=headers, data=json.dumps(payload), timeout=timeout)
    r.raise_for_status()
    data = r.json()
    raw = (data.get('data', {}).get('outputs', {}) or {}).get('result', '')
    try:
        return json.loads(raw)
    except Exception:
        pass
    # LLM이 JSON 뒤에 여분의 공백/문자를 덧붙이는 경우가 있어(raw.find('{')~rfind('}') 전체를
    # 파싱하면 "Extra data" 에러) 첫 '{'부터 시작하는 첫 번째 완전한 JSON 값만 디코드하고
    # 그 뒤는 버린다.
    s = raw.find('{')
    if s == -1:
        raise ValueError(f'JSON 파싱 실패: {raw[:200]}')
    try:
        obj, _ = json.JSONDecoder().raw_decode(raw, s)
        return obj
    except Exception:
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
    """dir_path 밑의 날짜별 *.jsonl(레코드 1개=1줄)을 파일명(=날짜) 순으로 이어붙여
    하나의 리스트로 반환. 옛 *.json(배열 하나를 통째로 담은 형식)이 남아있으면 그것도
    같이 읽어서(과도기적 호환) 마이그레이션을 깜빡해도 데이터가 안 보이는 일이 없게 한다."""
    if not dir_path.exists():
        return []
    out = []
    for f in sorted(dir_path.glob('*.jsonl')):
        with open(f, encoding='utf-8') as fh:
            for line in fh:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
    for f in sorted(dir_path.glob('*.json')):
        out.extend(load_json(f))
    return out


def dump_jsonl_sharded(dir_path, records, date_fn, only_keys=None):
    """records를 date_fn(record) 기준으로 묶어 dir_path/<날짜>.jsonl에 레코드 1개당 1줄로
    저장한다(매 실행마다 전체를 처음부터 다시 계산하는 단계 전용 — 매번 통째로 다시 쓰므로
    growing-append의 이점은 없지만, 한 줄에 레코드 하나씩이라 grep/head로 사람이 바로
    들여다볼 수 있다). only_keys를 주면 그 날짜들만 다시 쓴다."""
    buckets = {}
    for r in records:
        buckets.setdefault(date_fn(r), []).append(r)
    for k in (only_keys if only_keys is not None else buckets.keys()):
        path = dir_path / f'{k}.jsonl'
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + '.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            for r in buckets.get(k, []):
                f.write(json.dumps(r, ensure_ascii=False) + '\n')
        os.replace(tmp, path)


def clear_json_dir(dir_path):
    """매 실행마다 전체를 처음부터 다시 계산하는 단계(transform.py, resolve_links의 최종
    산출물)에서 쓴다 — 재계산 결과 특정 날짜에 해당하는 레코드가 하나도 안 남으면, 그 날짜의
    옛 파일이 갱신되지 않고 그대로 남아 stale 데이터가 되는 걸 막기 위해 먼저 비운다."""
    if dir_path.exists():
        for f in dir_path.glob('*.jsonl'):
            f.unlink()
        for f in dir_path.glob('*.json'):
            f.unlink()


def load_jsonl(path):
    """한 줄에 레코드 하나씩(append-only) 저장된 체크포인트를 읽는다. 같은 key가 여러 번
    나오면(재실행으로 재해석한 경우) 마지막 줄이 이긴다 — append만 하고 옛 줄을 지우지
    않으므로 "최신이 마지막"이라는 전제가 항상 성립한다."""
    if not path.exists():
        return {}
    out = {}
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            out[rec['key']] = rec
    return out


def append_jsonl(path, record):
    """레코드 1개를 파일 끝에 한 줄 추가한다 — 전체를 다시 쓰지 않으므로 파일이 아무리
    커져도(수만 건) 이 호출의 비용은 항상 거의 똑같다. link_resolution.json처럼 계속
    쌓이기만 하는 key-value 체크포인트가 건수가 늘수록 매 저장마다 전체를 다시 직렬화해서
    점점 느려지던 문제(실측 확인, 2026-07-27 — 1만 건에서 저장 1회 11.7초, 그 시간 동안
    다른 워커들도 lock 때문에 같이 멈춤)의 근본 해결책. os.replace 방식의 원자적 교체와는
    달리 append는 OS 레벨에서 한 줄 단위로는 안전하다고 보고 별도 임시파일을 안 쓴다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'a', encoding='utf-8') as f:
        f.write(json.dumps(record, ensure_ascii=False) + '\n')
