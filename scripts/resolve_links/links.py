"""페이지/링크인바이오 허브에서 "구매 링크 후보" 목록을 뽑는 로직(시도 순서 결정은 ranking.py)."""
import re
import threading

import linkbio_parser

from .config import BAD_DOMAINS, MAX_CANDIDATES, NON_PRODUCT_TEXT

# 같은 인플루언서의 링크인바이오 페이지(예: 인포크 계정 하나)를 형제 상품 여러 개가 그대로
# 공유하는 경우가 많아서(실측, 2026-07-27 — 남은 4720건이 유니크 URL 1710개뿐, 평균 2.7배
# 중복) 워커/스레드가 몇 개든 프로세스 전체에서 URL 하나당 한 번만 실제로 요청한다.
# 진행 중에 다른 스레드가 먼저 요청 중이면 그 결과를 기다렸다가 그대로 재사용한다(동시에
# 같은 URL을 여러 번 두들기는 걸 막기 위함 — thundering herd 방지).
_linkbio_cache = {}
_linkbio_cache_lock = threading.Lock()


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
    첫 요청만 실제로 계산하고 나머지는 그 결과를 기다렸다가 재사용한다."""
    with _linkbio_cache_lock:
        entry = _linkbio_cache.get(url)
        if entry is None:
            entry = {'event': threading.Event(), 'result': None}
            _linkbio_cache[url] = entry
            is_new = True
        else:
            is_new = False
    if not is_new:
        entry['event'].wait()
        return entry['result']
    try:
        entry['result'] = _fetch_linkbio_candidates(url)
    finally:
        entry['event'].set()
    return entry['result']


def _fetch_linkbio_candidates(url):
    """인포크/litt.ly/linktree 등 알려진 링크인바이오 플랫폼이면, Playwright로 렌더링하는
    대신 개발자가 공유해준 linkbio_parser로 requests 기반 구조화 데이터(상품명/가격/실제
    URL)를 직접 뽑아온다 — 브라우저 없이 훨씬 빠르고, 버튼 텍스트 추측 대신 실제 상품 목록을
    쓰니 더 정확하다(실측: viki105 계정 56개 상품을 2.5초에 정확한 이름+URL로 추출, 2026-07-20).
    지원 안 하는 플랫폼이거나 파싱 실패(페이지 구조 변경 등)면 None을 반환해 호출부가 기존
    Playwright 경로로 자연스럽게 넘어가게 한다."""
    try:
        linkbio_parser.detect_platform(url)
    except ValueError:
        return None
    try:
        data = linkbio_parser.parse(url, resolve_links=True)
    except Exception:
        return None

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
    return _filter_link_pairs(pairs)
