"""파이프라인 전체가 공유하는 설정/DB 연결/LLM 호출 헬퍼."""
import atexit
import datetime
import json
import os
import pathlib
import threading

import pymysql
import requests
from dotenv import load_dotenv

ROOT = pathlib.Path(__file__).resolve().parents[2]  # gonggu/infra/common.py -> 저장소 루트
load_dotenv(ROOT / '.env')

# crawl_pool.py의 스톨 워치독이 "드라이버 먹통"으로 판단해 이 단계 프로세스를 강제 종료할 때 쓰는
# exit code — daily.py가 이 코드로 실패한 단계만(=진짜 설정/코드 오류가 아니라 크롤 풀이 멈춘
# 경우만) 자동으로 --from 재개를 대신 해준다(2026-08-18, daily 자동 재시도). 두 파일이 각자
# 하드코딩하면 한쪽만 고쳤을 때 조용히 어긋나므로 여기 한 곳에만 정의한다.
CRAWL_STALL_EXIT_CODE = 3

# crawl_pool.py가 CRAWL_RECYCLE_SEC 경과 후 "의도된 정기 재기동"으로 스스로 종료할 때 쓰는 exit
# code(2026-08-18, 속도개선 공사 실측 근거) — 사용자가 직접 관찰: 오래 켜둔 브라우저 풀(같은
# Playwright 브라우저를 재사용하며 수백 개 사이트를 오간)이 5분쯤 지나면 처리 속도가 눈에 띄게
# 떨어지는데, 껐다 켜면(새 브라우저로) 다시 빨라진다 — 브라우저 하나가 오래 살수록 메모리를
# 누적해(실측 확인된 스왑 사용량 증가와 일치) 시스템 전체가 느려지는 것으로 추정된다. 매번 사람이
# 손으로 끄고 켜는 대신, 이 exit code로 daily가 "실패"가 아니라 "건강한 정기 재시작"으로 알아보고
# 무제한(재시도 횟수 차감 없이) 자동으로 이어서 재개한다 — CRAWL_STALL_EXIT_CODE(진짜 먹통, 제한된
# 횟수만 재시도)와는 의미가 다르므로 값도 별도로 둔다.
CRAWL_RECYCLE_EXIT_CODE = 4


def acquire_lock(name):
    """중복 실행 방지 락 — 이미 살아있는 동일 이름 실행이 있으면 SystemExit로 시작을 거부한다.

    크롤 무거운 단계(rescan/resolve/backfill/enrich/reverify/crawl_linkbio)를 실수로 겹쳐
    돌리면 브라우저가 배수로 떠 메모리·안티봇 과부하가 난다(2026-08-11 rescan 5중첩으로 크롬
    수백 개·스왑 소진, uc 크롬까지 못 뜬 사고). daily는 자체 lock이 있지만 개별 단계를 수동/
    --from으로 다시 돌리면 이전 것이 고아로 살아있는 채 새로 쌓인다 — 이 락이 그 두 번째를 막는다.

    인터프리터 정상 종료(예외·Ctrl-C 포함) 시 atexit로 lock을 지운다. -9로 강제 종료돼 lock이
    남으면 다음 실행이 pid 생존(os.kill 0)을 확인해 죽은 lock은 덮어쓴다(daily._acquire_lock 패턴)."""
    lock = ROOT / f'data/output/.{name}.lock'
    if lock.exists():
        try:
            pid = int(lock.read_text().strip())
            os.kill(pid, 0)  # 살아있으면 예외 없음
        except (ValueError, ProcessLookupError, PermissionError):
            pass  # 죽은 프로세스의 잔여 lock — 덮어쓴다
        else:
            raise SystemExit(
                f'⚠ 이미 {name}이(가) 실행 중입니다(pid {pid}). 중복 실행은 브라우저·메모리·'
                f'안티봇 과부하를 유발하므로 시작을 거부합니다. 그 실행이 끝난 뒤 다시 시도하세요'
                f'(멈춘 것 같으면: pkill -f "{name}" 로 정리 후 재시도).')
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(str(os.getpid()))
    atexit.register(lambda: lock.unlink() if lock.exists() else None)

# 각 단계 산출물을 발행일(YYYY-MM-DD.json)별로 쪼개서 폴더에 저장한다 — 폴더 이름 자체가
# "이 파일이 몇 번째 단계에서 나왔는지"를 보여주고, 날짜 파일 하나만 열어도 그날 무슨 포스트가
# 어느 단계까지 갔는지 바로 확인할 수 있다. resolve_links 내부의 상품 단위 체크포인트
# (link_resolution.json)는 날짜 필드가 없는 key-value 저장이라 그대로 단일 파일로 둔다.
RAW_DIR = ROOT / 'data/01_raw'
CLASSIFIED_DIR = ROOT / 'data/02_classified'
LOAD_READY_DIR = ROOT / 'data/03_load_ready'
RESOLVED_DIR = ROOT / 'data/04_resolved'

# classify.py/classify_yt_ppl.py가 "이미 분류 성공한 key"인지 확인할 때 쓰는 작은 인덱스
# (2026-08-18 점검, 문제 1/9) — CLASSIFIED_DIR 전체(실측 223MB+, 하루 5~12MB씩 증가)를 매일
# 두 스크립트가 각자 다시 파싱하던 걸 대체한다. 두 스크립트가 다루는 key 공간은 fetch 단계
# SQL에서부터 서로 배타적이라(같은 post_id/video_id가 양쪽에 동시에 걸릴 수 없음) 하나의
# 파일을 공유해도 안전하다. load_classify_done_keys/record_classify_done_key 참고.
CLASSIFY_DONE_KEYS_FILE = ROOT / 'data/output/classify_done_keys.jsonl'

# prompts.CATEGORY_CLASSIFY_SYSTEM이 이 dict에서 카테고리 목록을 동적으로 생성하므로 여기만
# 고치면 프롬프트도 자동으로 맞춰진다. classify_category.py/category_dashboard.py도 공유해서
# 쓴다(따로 들고 있으면 서로 어긋날 수 있어서 한 곳에 둠).
CATEGORY_TAXONOMY = {
    '뷰티': ['스킨케어(스킨/로션/크림/세럼/앰플 등)', '클렌징', '선케어', '마스크팩', '메이크업', '헤어/바디', '향수', '뷰티기기', '네일', '기타'],
    '식품': ['반찬/국/밀키트', '간식/디저트', '과일/채소/곡물', '간편식/분식', '오일/양념/육수', '음료/차/커피', '정육/수산', '유제품/버터/계란', '기타'],
    '영양제': ['비타민/미네랄', '유산균/장건강', '오메가3', '콜라겐/이너뷰티', '효소/발효', '건강즙/시럽', '면역/수면/눈건강', '기타'],
    '다이어트/헬스': ['프로틴/쉐이크', '다이어트보조제', '식단관리식품', '홈트/헬스기구', '운동보조용품', '바디관리기기', '운동복/슬리밍웨어', '기타'],
    '육아': ['분유/이유식', '기저귀/물티슈', '키즈의류', '유아용품', '완구/교육', '유모차/카시트', '임산부/임부복', '유아동도서', '기타'],
    '패션': ['원피스/세트', '티셔츠/블라우스/니트', '기타상의', '하의(팬츠/스커트/데님)', '아우터', '이너/속옷', '홈웨어/잠옷', '기타'],
    '신발/가방/악세서리': ['운동화', '구두/부츠', '슬리퍼/샌들', '여성가방', '남성가방/백팩', '주얼리', '시계', '모자/벨트/우산', '악세서리', '기타'],
    '살림/청소': ['수납/정리/그릇', '세탁/세제', '청소용품', '욕실', '방향/탈취·제습', '휴지/생활소모품', '구강/위생', '기타'],
    '주방': ['냄비/프라이팬', '밀폐용기/주방수납', '칼/도마/조리도구', '식기/컵/텀블러', '주방가전', '세제/소모품', '싱크볼/수전/건조대', '기타'],
    '가전/디지털': ['생활가전', '계절가전', '음향기기', '모바일 액세서리', 'PC/주변기기', '프린터', '기타'],
    '인테리어': ['침구/패브릭', '커튼/블라인드', '러그', '조명', '가구', '홈데코', '디퓨저/캔들', '식물/원예', '집수리/시공', '기타'],
    '반려동물': ['사료/간식', '배변용품', '장난감', '미용/위생', '하네스/이동장', '영양제', '의류', '식기/급수', '하우스/침구', '기타'],
    '스포츠/취미': ['등산/트레킹', '자전거/라이딩', '골프', '수영', '구기종목', '캠핑/차박', '문구/도서', '자동차용품', '보드게임/취미', '기타'],
    '여행/숙박': ['국내숙박', '해외여행', '워터파크/테마파크', '캐리어/여행용품', '레저/액티비티', '기타'],
    '교육/클래스': ['영어학습', '코딩/STEM', '원서/독서구독', '학습교재/전집', '자기계발', '잡지/콘텐츠', '기타'],
    '기타': [],
}

# LLM이 category 필드에 subcategory 문자열을 잘못 넣는 경우(예: category="여행/캐리어")를
# 자동으로 바로잡기 위한 역방향 조회 테이블. "기타"는 여러 대카테고리에 공통 하위카테고리로
# 들어있어 여기서는 유일하게 안 정해지지만, "기타"는 그 자체로 16번 대카테고리이기도 해서
# category 필드에 그대로 나와도 이미 유효한 값이라 이 역방향 조회를 탈 일이 없다(무해함).
# 그 외 하위카테고리 문자열은 카테고리 간에 겹치지 않는다(직접 대조 확인함).
SUBCATEGORY_TO_CATEGORY = {
    sub: cat for cat, subs in CATEGORY_TAXONOMY.items() for sub in subs
}

DEEPSEEK_URL = os.environ.get('DEEPSEEK_URL', 'https://api.deepseek.com').rstrip('/')
DEEPSEEK_KEY = os.environ.get('DEEPSEEK_KEY', '')
DEEPSEEK_MODEL = os.environ.get('DEEPSEEK_MODEL', 'deepseek-v4-pro')
# classify_category.py의 2단 캐스케이드(플래시로 1차 스크리닝 → confidence 낮은 것만 프로로
# 재검증)에서 1차용으로 쓰는 저렴한 모델.
DEEPSEEK_MODEL_FLASH = os.environ.get('DEEPSEEK_MODEL_FLASH', 'deepseek-v4-flash')

# classify.py/resolve_links가 스레드풀로 동시에 call_llm을 부르므로, 매 호출마다 새
# TCP/TLS 커넥션을 맺지 않도록 세션을 공유한다(requests.Session은 스레드 간 공유 안전 —
# 내부 urllib3 커넥션 풀이 스레드 세이프). pool_maxsize는 실측한 최대 동시성보다 넉넉하게
# 잡아 풀 부족으로 인한 새 연결 생성을 막는다 — DeepSeek 직접 호출로 전환 후 동시 400도
# 429 없이 통과하는 걸 확인해서(2026-07-28), 예전 Dify/Cloudflare 시절 상한(약 48) 기준이던
# 값을 올려둔다.
_session = requests.Session()
_adapter = requests.adapters.HTTPAdapter(pool_connections=256, pool_maxsize=256)
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


# call_llm이 스레드 수십~수백 개에서 동시에 불려서(classify.py는 CONCURRENCY=200까지) 사용량
# 로그 파일에 append_jsonl을 락 없이 그대로 호출하면 줄이 섞여 깨질 수 있다(같은 이유로
# classify.py도 자기 체크포인트 append를 lock으로 감쌈, classify.py:99-112 참고) — 이 로그
# 전용 락을 따로 둔다.
_usage_lock = threading.Lock()
LLM_USAGE_FILE = ROOT / 'data/output/llm_usage.jsonl'

# resolve_links가 링크인바이오 허브를 파싱하다 곁다리로 찾은 인스타그램 계정 이메일 —
# user_id(=hifen instagram_user.user_id)당 한 줄, key당 마지막 줄이 최신(common.load_jsonl
# 규약). sync_hifen_emails.py가 이 파일을 읽어 hifen(SRC) DB에 반영한다. dev_gongguking에는
# 이메일 컬럼이 없고 앞으로도 안 만든다 — 이 파일이 유일한 로컬 축적처.
HIFEN_EMAIL_FILE = ROOT / 'data/output/hifen_emails.jsonl'


def _log_usage(model, usage):
    """DeepSeek 응답의 usage 필드(토큰 수)를 그대로 파일에 남긴다. 단가(원/달러)는 여기서
    계산하지 않는다 — 이 파이프라인이 쓰는 모델명(deepseek-v4-pro 등)이 DeepSeek 공개 요금표의
    표준 모델명과 다를 수 있어(내부 게이트웨이/별칭 가능성), 잘못된 단가를 여기 하드코딩해서
    틀린 비용을 보여주는 것보다 토큰 수만 정확히 남기는 쪽을 택한다 — 비용 계산은
    llm_usage_report.py에서 단가를 알 때만 선택적으로 한다."""
    if not usage:
        return
    entry = {
        # 타임존 오프셋을 붙인다(2026-08-19 요금 개편) — 단가가 UTC 시각 기준 피크/오프피크로
        # 2배 갈리므로, naive 로컬 시각만 남기면 리포트가 머신 타임존을 추측해야 한다.
        # 앞부분은 여전히 로컬 날짜라 llm_usage_report의 날짜 필터(startswith)는 그대로 동작한다.
        'ts': datetime.datetime.now().astimezone().isoformat(timespec='seconds'),
        'model': model,
        'prompt_tokens': usage.get('prompt_tokens'),
        'completion_tokens': usage.get('completion_tokens'),
        'total_tokens': usage.get('total_tokens'),
        'cache_hit_tokens': usage.get('prompt_cache_hit_tokens'),
        'cache_miss_tokens': usage.get('prompt_cache_miss_tokens'),
    }
    with _usage_lock:
        append_jsonl(LLM_USAGE_FILE, entry)


def call_llm(system_prompt, user_message, timeout=120, model=None):
    """DeepSeek chat completions 호출 — system/user 메시지 2개를 보내고 응답 텍스트를 JSON으로
    파싱해서 돌려준다. response_format을 json_object로 강제해도 모델이 앞뒤에 여분의 텍스트를
    붙이는 경우가 있어 파싱 폴백을 둔다. model을 안 주면 기본(프로) 모델을 쓴다 — 캐스케이드처럼
    호출마다 다른 모델을 써야 하는 경우에만 명시적으로 넘긴다.

    ⚠ timeout 기본값 120초(2026-08-04 변경, 원래 60초) — 플래시 모델은 가끔 호출 하나가
    60~117초까지 걸리는 꼬리 지연이 있다고 이미 실측돼 있는데(resolve_links/config.py의
    LINK_LLM_MODEL 관련 기록 참고), DEEPSEEK_MODEL을 전부 플래시로 돌리기 시작하면서 60초
    타임아웃에 대량으로 걸려 'Read timed out' 에러가 쏟아지는 게 실제로 확인됨. 60초 근처에서
    끝났을 응답까지 억지로 죽이지 않으려고 여유를 둔다."""
    headers = {'Authorization': f'Bearer {DEEPSEEK_KEY}', 'Content-Type': 'application/json'}
    payload = {
        'model': model or DEEPSEEK_MODEL,
        'messages': [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_message},
        ],
        'response_format': {'type': 'json_object'},
    }
    r = _session.post(f'{DEEPSEEK_URL}/chat/completions', headers=headers, data=json.dumps(payload), timeout=timeout)
    r.raise_for_status()
    data = r.json()
    _log_usage(payload['model'], data.get('usage'))
    raw = data['choices'][0]['message']['content'] or ''
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


def _classify_key(record):
    native_id = record.get('post_id') if record.get('platform') == 'ig' else record.get('video_id')
    return f"{record.get('platform')}:{native_id}" if native_id else None


def _bootstrap_classify_done_keys():
    """CLASSIFY_DONE_KEYS_FILE이 아직 없을 때(최초 실행) 딱 한 번 CLASSIFIED_DIR 전체를 훑어
    지금까지 성공 분류된 key를 모은다 — 이후로는 이 비용을 다시 치르지 않는다."""
    keys = set()
    for r in load_json_dir(CLASSIFIED_DIR):
        if r.get('classification') and not r.get('classification_error'):
            key = _classify_key(r)
            if key:
                keys.add(key)
    return keys


def load_classify_done_keys():
    """classify.py/classify_yt_ppl.py가 공유하는 '이미 분류 성공' key 집합(2026-08-18, 문제
    1/9). 파일이 있으면 그것만 읽는다(작고 하루 증가분만큼만 자람). 없으면(최초 실행)
    CLASSIFIED_DIR 전체를 한 번 훑어 부트스트랩하고 파일로 남긴 뒤 돌려준다."""
    if not CLASSIFY_DONE_KEYS_FILE.exists():
        keys = _bootstrap_classify_done_keys()
        CLASSIFY_DONE_KEYS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(CLASSIFY_DONE_KEYS_FILE, 'w', encoding='utf-8') as f:
            for k in sorted(keys):
                f.write(json.dumps({'key': k}, ensure_ascii=False) + '\n')
        return keys
    return set(load_jsonl(CLASSIFY_DONE_KEYS_FILE).keys())


def record_classify_done_key(key):
    """key 하나를 CLASSIFY_DONE_KEYS_FILE에 append — classify.py/classify_yt_ppl.py가 성공
    분류 결과를 CLASSIFIED_DIR에 저장하는 바로 그 자리에서 같이 호출한다."""
    append_jsonl(CLASSIFY_DONE_KEYS_FILE, {'key': key})
