"""브라우저 없이 requests+bs4로 페이지 판별 정보를 뽑는 패스트패스.

browser.fetch()가 돌려주는 것과 똑같은 모양의 rec를 만들어주고, 정보가 부족하거나 차단된
낌새가 있으면 None을 돌려 호출부가 기존 Playwright 경로로 그대로 넘어가게 한다.

왜 필요한가: LLM#3 판별에 실제로 쓰는 건 title / og:image 유무 / JSON-LD / 본문 텍스트
2000자뿐인데, 이걸 얻자고 Chromium을 띄우면 건당 4초 안팎이 든다(실측, 2026-08-01 —
브라우저 기동 3.9초 + fetch 3~4초). 같은 페이지를 requests로 받으면 0.1~0.3초다.

실측(2026-08-01, 실제 재검증 도착 URL 표본):
  shop.srookpay.com  0.27s 200 og:image O   ← 브라우저 재검증의 24%. Playwright로는 403이 났다
  store.kakao.com    0.24s 200 og:image O
  sanjitalk.com      0.10s 200 og:image O
  smartstore.naver.com / brand.naver.com    429 → 브라우저 필요(BROWSER_ONLY_HOSTS)
"""
import json
import re
import threading

import requests
from bs4 import BeautifulSoup

from .config import (BLOCKED_STATUS_CODES, BLOCKED_TEXT_MARKERS, BROWSER_ONLY_HOSTS,
                      HTTP_FAST_PATH, HTTP_FETCH_TIMEOUT, HTTP_MIN_BODY_TEXT,
                      RESOLVE_CONCURRENCY, UA)
from .urlutil import host_of

# 워커 스레드들이 공유한다(requests.Session은 스레드 세이프). 매 호출마다 TLS 핸드셰이크를
# 새로 하면 0.3초짜리 요청이 1초가 되므로 커넥션 풀을 워커 수만큼 넉넉히 잡는다.
_session = requests.Session()
_adapter = requests.adapters.HTTPAdapter(pool_connections=64,
                                          pool_maxsize=max(64, RESOLVE_CONCURRENCY))
_session.mount('https://', _adapter)
_session.mount('http://', _adapter)

# 패스트패스가 실제로 몇 번 먹혔는지 + 안 먹혔으면 왜인지. "브라우저는 거의 안 뜬다"는 가정이
# 코드 주석에만 남고 실제로는 무너져 있었던 게 이번 성능 저하의 원인이라(2026-08-01), 이번엔
# 매 실행마다 실측치가 찍히게 해둔다 — 폴백 사유별로 봐야 "어느 도메인 때문에 느린지"가 보인다.
_STATS_LOCK = threading.Lock()
_stats = {'tried': 0, 'hit': 0}


def stats():
    with _STATS_LOCK:
        return dict(_stats)


def _bump(field):
    with _STATS_LOCK:
        _stats[field] = _stats.get(field, 0) + 1


def _fallback(reason):
    """폴백 사유를 세고 None을 돌려준다 — 호출부는 return _fallback(...) 한 줄로 끝난다."""
    _bump('miss:' + reason)
    return None


def _meta(soup, prop):
    el = soup.find('meta', attrs={'property': prop}) or soup.find('meta', attrs={'name': prop})
    return el.get('content') if el else None


def extract_jsonld_blocks(blocks):
    """<script type="application/ld+json"> 안의 원문 문자열들에서 Product 하나를 찾아낸다.
    browser.py(Playwright 경로)와 여기(requests 경로)가 같은 판별 근거를 쓰도록 파싱 규칙을
    한 곳에만 둔다 — 두 경로가 서로 다른 결과를 내면 "브라우저로 다시 열면 판정이 바뀌는"
    재현 안 되는 버그가 된다."""
    for raw in blocks:
        try:
            data = json.loads((raw or '').strip())
        except Exception:
            continue
        items = data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])
        if isinstance(data, dict) and '@graph' in data:
            items = data['@graph']
        for it in items:
            if not isinstance(it, dict):
                continue
            t = it.get('@type', '')
            t = t if isinstance(t, str) else ','.join(t)
            if 'Product' in t:
                img = it.get('image')
                offers = it.get('offers') or {}
                if isinstance(offers, list):
                    offers = offers[0] if offers else {}
                return {'name': it.get('name'), 'image': img[0] if isinstance(img, list) else img,
                        'price': offers.get('price'), 'currency': offers.get('priceCurrency')}
    return {}


def extract_jsonld(html):
    """HTML 문자열용 래퍼(Playwright 경로에서 사용)."""
    return extract_jsonld_blocks(
        m.group(1) for m in re.finditer(
            r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, re.S))


# 인라인 스타일로 숨겨둔 요소를 찾아내는 패턴. 브라우저의 inner_text()는 "보이는 텍스트"만
# 주는데 bs4의 get_text()는 숨겨진 것까지 전부 준다 — 이 차이 때문에 실제 사고가 났다(실측
# 확인, 2026-08-01 — shop.byulnanmam.com은 display:none인 "친구 초대 리워드" 모달이 DOM
# 앞쪽에 있어서, 2000자를 그 안내문이 다 잡아먹고 정작 상품명·가격(59,800원)이 잘려나갔다.
# LLM#3이 근거를 못 봐서 done이 unresolved로 뒤집혔다).
_HIDDEN_STYLE = re.compile(r'display\s*:\s*none|visibility\s*:\s*hidden', re.I)


def _strip_hidden(soup):
    """브라우저 inner_text()에 최대한 가깝게 — 안 보이는 요소를 본문에서 뺀다."""
    for t in soup(['script', 'style', 'noscript', 'template']):
        t.decompose()
    for el in soup.find_all(style=_HIDDEN_STYLE):
        el.decompose()
    for el in soup.find_all(hidden=True):
        el.decompose()
    for el in soup.find_all(attrs={'aria-hidden': 'true'}):
        el.decompose()


_PRICE_RE = re.compile(r'\d{1,3}(?:,\d{3})+\s*원')


def _snippet(text, limit=2000):
    """LLM#3에 넘길 본문 2000자를 고른다.

    앞에서부터 자르면 안 되는 이유: 스타일시트 클래스로 숨긴 대형 카테고리 내비게이션은
    bs4가 "숨김"인 줄 알 방법이 없어서(인라인 style만 걸러낼 수 있다) 본문 앞부분을 통째로
    잡아먹는다 — 실측 확인(2026-08-01, www.foodshop.co.kr): 앞 2000자가 전부 메뉴라서
    정작 상품명·가격이 잘려나가 LLM#3이 done을 unresolved로 뒤집었다. 가격 표기가 창 밖에
    있으면 그 근처로 창을 옮겨서, 판별 근거가 되는 구간이 반드시 포함되게 한다."""
    if len(text) <= limit:
        return text
    m = _PRICE_RE.search(text, limit)
    if not m:
        return text[:limit]
    start = max(0, m.start() - limit // 4)
    return text[start:start + limit]


def _is_browser_only(url):
    host = host_of(url)
    return any(host == h or host.endswith('.' + h) for h in BROWSER_ONLY_HOSTS)


def try_http_fetch(url, referer=None):
    """성공하면 browser.fetch()와 같은 모양의 rec, 브라우저가 필요하면 None.

    None을 돌리는 조건(전부 "requests로는 판단 근거가 모자라다"는 뜻):
    - 네이버 등 bare requests에 429를 주는 호스트(BROWSER_ONLY_HOSTS)
    - HTTP 4xx/5xx, HTML이 아닌 응답, 네트워크 실패
    - 본문에 안티봇/캡차 문구 — 브라우저는 저장된 세션 쿠키+stealth로 통과할 수도 있으므로
      여기서 차단으로 확정하지 않고 브라우저에 넘긴다
    - title이 없거나, JSON-LD 상품명도 og:image도 없는 경우 — JS로 그려지는 껍데기 HTML일
      가능성이 커서, 이대로 LLM#3에 넘기면 브라우저로 열었을 때와 다른 판정이 나온다
    - 본문 텍스트가 HTTP_MIN_BODY_TEXT자 미만 — og/title은 서버가 SEO용으로 넣어주지만 본문은
      전부 JS로 그리는 몰이 실제로 있다(실측, 2026-08-01 — store.kakao.com은 og:image·og:title이
      다 있는데 body 텍스트가 0자). LLM#3은 "정가 238,000 공구가 166,600"처럼 본문에만 있는
      가격을 판별 근거로 쓰므로, 본문이 비면 브라우저로 열었을 때와 판정이 달라진다.
    """
    if not HTTP_FAST_PATH:
        return None
    if _is_browser_only(url):
        return _fallback('browser_only_host')
    _bump('tried')
    headers = {'User-Agent': UA, 'Accept-Language': 'ko-KR,ko;q=0.9,en;q=0.8',
               'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'}
    if referer:
        headers['Referer'] = referer
    try:
        r = _session.get(url, headers=headers, timeout=HTTP_FETCH_TIMEOUT, allow_redirects=True)
    except Exception as e:
        return _fallback('request_failed:' + type(e).__name__)
    if r.status_code in BLOCKED_STATUS_CODES:
        return _fallback(f'http_{r.status_code}')
    if r.status_code >= 400:
        return _fallback('http_4xx_5xx')
    if 'html' not in (r.headers.get('Content-Type') or 'text/html').lower():
        return _fallback('not_html')

    # 한국 쇼핑몰엔 euc-kr 페이지가 아직 남아 있어서 r.text(헤더 기반 추정)를 믿으면 제목이
    # 깨진다 — bs4가 <meta charset>까지 보고 판별하도록 원본 바이트를 그대로 넘긴다.
    soup = BeautifulSoup(r.content, 'lxml')
    jsonld = extract_jsonld_blocks(
        s.string or '' for s in soup.find_all('script', attrs={'type': re.compile(r'ld\+json', re.I)}))
    og_image = _meta(soup, 'og:image')
    title = _meta(soup, 'og:title') or (soup.title.get_text(strip=True) if soup.title else None)
    _strip_hidden(soup)
    body = soup.body or soup
    body_text = _snippet(body.get_text(' ', strip=True))

    if any(m.lower() in body_text.lower() for m in BLOCKED_TEXT_MARKERS):
        return _fallback('antibot_text')
    if not title:
        return _fallback('no_title')
    if not (jsonld.get('name') or og_image):
        return _fallback('no_jsonld_or_og')
    if len(body_text) < HTTP_MIN_BODY_TEXT:
        return _fallback('body_too_short')

    _bump('hit')
    return {'status': r.status_code, 'final_url': r.url, 'title': title, 'og_image': og_image,
            'jsonld': jsonld, 'body_text': body_text, 'error': None, 'via': 'http'}
