"""페이지/링크인바이오 허브에서 "구매 링크 후보" 목록을 뽑는 로직(시도 순서 결정은 ranking.py)."""
import re
import threading

from gonggu import linkbio_parser
from gonggu.common import ROOT, append_jsonl, load_jsonl

from .antibot import is_excluded_marketplace
from .config import BAD_DOMAINS, MAX_CANDIDATES, NON_PRODUCT_TEXT

# 같은 인플루언서의 링크인바이오 페이지(예: 인포크 계정 하나)를 형제 상품 여러 개가 그대로
# 공유하는 경우가 많아서(실측, 2026-07-27 — 남은 4720건이 유니크 URL 1710개뿐, 평균 2.7배
# 중복) 워커/스레드가 몇 개든 프로세스 전체에서 URL 하나당 한 번만 실제로 요청한다.
# 진행 중에 다른 스레드가 먼저 요청 중이면 그 결과를 기다렸다가 그대로 재사용한다(동시에
# 같은 URL을 여러 번 두들기는 걸 막기 위함 — thundering herd 방지).
_linkbio_cache = {}
_linkbio_cache_lock = threading.Lock()

# 파싱 결과의 영구 저장소(2026-08-18 점검, 문제 8 수정) — 위 _linkbio_cache는 프로세스 메모리
# 안에서만 산다. RESOLVE_SHARD_COUNT>1이면 runner.finalize()가 전 샤드 종료 후 완전히 새
# 프로세스(`--finalize`)에서 한 번 불리는데, 그 프로세스의 _linkbio_cache는 항상 비어있어서
# runner._dump_linkbio가 조용히 0건을 만들었다. 허브 파싱에 성공하는 즉시(어느 프로세스든)
# 이 append-only 파일에도 같이 남겨서, _dump_linkbio가 프로세스 경계와 무관하게 지금까지
# 파싱된 전체를 볼 수 있게 한다(RESOLUTION_FILE과 동일한 key-append-only 체크포인트 패턴).
LINKBIO_HUB_CACHE_FILE = ROOT / 'data/output/linkbio_hub_cache.jsonl'


def normalize_url(u):
    """캡션 원문에서 그대로 뽑힌 URL이라 콜론 빠짐(https//...)이나 스킴 없음, 중복 스킴 같은
    오타가 섞여 있을 수 있어 fetch 전에 보정한다."""
    u = (u or '').strip()
    if not u:
        return u
    u = re.sub(r'(https?)//', r'\1://', u)
    matches = list(re.finditer(r'https?://', u))
    if len(matches) > 1:
        u = u[matches[-1].start():]
    if not re.match(r'^https?://', u):
        u = 'https://' + u
    u = re.sub(r'^https?://blog\.naver\.com/', 'https://m.blog.naver.com/', u)
    return u


def _filter_link_pairs(pairs):
    """(href, text, source) 목록에서 BAD_DOMAINS/NON_PRODUCT_TEXT/중복을 걸러 {href, text,
    source} 후보로 정리한다 — extract_collection_links와 linkbio_candidates가 공유하는
    필터. source='product'는 smart_store/collection처럼 실제 상품명·가격이 있는 구조화
    데이터, source='link'는 그냥 버튼/링크 하나(스토어 메인이나 또 다른 링크모음일 수도
    있어 신뢰도가 낮음) — picker.finalize_pick에서 이 구분에 따라 검증 강도를 다르게 적용한다."""
    out, seen = [], set()
    for href, text, source in pairs:
        if not href or href in seen or any(d in href for d in BAD_DOMAINS):
            continue
        if is_excluded_marketplace(href):   # 쿠팡/알리/테무 링크는 후보에서 원천 제외(2026-08-11)
            continue
        text_norm = re.sub(r'\s+', '', text or '').lower()
        if text_norm and any(kw in text_norm for kw in NON_PRODUCT_TEXT):
            continue
        seen.add(href)
        out.append({'href': href, 'text': text, 'source': source})
        if len(out) >= MAX_CANDIDATES:
            break
    return out


def extract_collection_links(page):
    try:
        raw = page.eval_on_selector_all(
            'a[href]', "els => els.map(e => ({href: e.href, text: e.innerText.trim()}))")
    except Exception:
        return []
    # 같은 페이지 안의 앵커/네비게이션 링크(fragment만 다르거나 완전히 같은 URL)는 실제 이동이
    # 아니니 후보에서 뺀다 — LLM#2가 이런 걸 최종 링크로 잘못 고르는 걸 방지.
    current_no_frag = page.url.split('#')[0]
    pairs = []
    for l in raw:
        href, text = l.get('href', ''), l.get('text', '')
        if not href or re.match(r'^(javascript|mailto|tel):', href, re.I):
            continue
        if href.split('#')[0] == current_no_frag:
            continue
        pairs.append((href, text, 'link'))
    return _filter_link_pairs(pairs)


def linkbio_candidates(url):
    """_fetch_linkbio_candidates의 URL 단위 캐시 래퍼. 같은 URL을 여러 상품이 동시에 요청하면
    첫 요청만 실제로 계산하고 나머지는 그 결과를 기다렸다가 재사용한다.
    파싱 원본(data)도 캐시에 함께 남겨, resolve가 끝난 뒤 load_persisted_linkbio_data로 꺼내
    날짜별 JSON으로 저장한다(인포크를 두 번 크롤하지 않기 위함). 파싱에 성공하면 프로세스 메모리뿐
    아니라 LINKBIO_HUB_CACHE_FILE에도 즉시 append한다 — 다른 프로세스(예: 샤딩된 실행의
    --finalize)가 이 프로세스의 메모리를 못 봐도 파싱 결과를 볼 수 있게 하기 위함(문제 8)."""
    with _linkbio_cache_lock:
        entry = _linkbio_cache.get(url)
        if entry is None:
            entry = {'event': threading.Event(), 'result': None, 'data': None}
            _linkbio_cache[url] = entry
            is_new = True
        else:
            is_new = False
    if not is_new:
        entry['event'].wait()
        return entry['result']
    try:
        entry['result'], entry['data'] = _fetch_linkbio_candidates(url)
        if entry['data'] is not None:
            append_jsonl(LINKBIO_HUB_CACHE_FILE, {'key': url, 'data': entry['data']})
    finally:
        entry['event'].set()
    return entry['result']


def load_persisted_linkbio_data():
    """LINKBIO_HUB_CACHE_FILE 전체를 {hub_url: 파싱 원본 dict}로 복원한다 — 프로세스 경계와
    무관하게(샤딩된 여러 프로세스가 각자 남긴 것 포함) 지금까지 파싱된 허브 전체를 본다.
    runner._dump_linkbio가 cached_linkbio_data(프로세스 로컬) 대신 이걸 쓴다(문제 8 수정)."""
    return {k: rec['data'] for k, rec in load_jsonl(LINKBIO_HUB_CACHE_FILE).items()}


_HUB_URL_RE = re.compile(r'https?://[^\s;]+')


def extract_linkbio_hub_urls(text):
    """candidate_url 원문(여러 후보가 세미콜론/공백으로 섞인 텍스트)에서 linkbio_parser가
    지원하는 플랫폼(인포크/링크트리/litt.ly 등, hosts.py 참고)의 허브 URL만 뽑는다.
    normalize_url까지 거쳐서 core.resolve_product가 linkbio_candidates(url)를 부를 때 쓰는
    키와 똑같이 맞춘다 — 그래야 load_persisted_linkbio_data()의 결과에서 그대로 찾을 수 있다
    (resolve_links/runner.py의 일일 이메일/허브 파싱본 저장이 재크롤 없이 이걸로 동작한다)."""
    hubs, seen = [], set()
    for raw in _HUB_URL_RE.findall(text or ''):
        u = normalize_url(raw)
        try:
            linkbio_parser.detect_platform(u)
        except ValueError:
            continue
        if u not in seen:
            seen.add(u)
            hubs.append(u)
    return hubs


def _fetch_linkbio_candidates(url):
    """인포크/litt.ly/linktree 등 알려진 링크인바이오 플랫폼이면, Playwright로 렌더링하는
    대신 개발자가 공유해준 linkbio_parser로 requests 기반 구조화 데이터(상품명/가격/실제
    URL)를 직접 뽑아온다 — 브라우저 없이 훨씬 빠르고, 버튼 텍스트 추측 대신 실제 상품 목록을
    쓰니 더 정확하다(실측: viki105 계정 56개 상품을 2.5초에 정확한 이름+URL로 추출, 2026-07-20).
    지원 안 하는 플랫폼이거나 파싱 실패(페이지 구조 변경 등)면 (None, None)을 반환해 호출부가 기존
    Playwright 경로로 자연스럽게 넘어가게 한다.
    반환: (후보 리스트, 파싱 원본 dict) — 원본은 LINKBIO_HUB_CACHE_FILE에 영구 저장되고
    load_persisted_linkbio_data로 노출돼 날짜별 JSON 저장에 쓰인다."""
    try:
        linkbio_parser.detect_platform(url)
    except ValueError:
        return None, None
    try:
        data = linkbio_parser.parse(url, resolve_links=True)
    except Exception:
        return None, None

    def _absolute(href):
        # resolved_url이 없으면(리다이렉트 추적 실패) url 원본으로 대체하는데, 인포크
        # 등에선 이 원본이 도메인 없는 상대경로(예: "/api/r/<토큰>")라 normalize_url이
        # "https://"만 앞에 붙이면 "https:///api/r/..."처럼 깨진 URL이 되어 그대로 done
        # 확정되는 사고가 났었다(실측 확인, 2026-07-20) — 절대 URL이 아니면 버린다.
        return href if href and re.match(r'^https?://', href) else None

    pairs = []
    for l in data.get('links') or []:
        href = _absolute(l.get('resolved_url') or l.get('url'))
        pairs.append((href, l.get('title') or '', 'link'))
    for s in data.get('smart_stores') or []:
        for p in s.get('products') or []:
            href = _absolute(p.get('resolved_url') or p.get('url'))
            price = p.get('sale_price') or p.get('discount_price')
            text = f"{p.get('name') or ''} {price}원".strip() if price else (p.get('name') or '')
            pairs.append((href, text, 'product'))
    for c in data.get('collections') or []:
        for p in c.get('products') or []:
            href = _absolute(p.get('resolved_url') or p.get('url'))
            price = p.get('price')
            text = f"{p.get('name') or ''} {price}원".strip() if price else (p.get('name') or '')
            pairs.append((href, text, 'product'))
    return _filter_link_pairs(pairs), data
