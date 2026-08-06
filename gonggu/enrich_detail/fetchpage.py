"""상품페이지 전체 HTML 가져오기 — requests 패스트패스 → 브라우저 폴백 + gone 판정.

resolve_links의 httpfetch/browser는 "판별에 필요한 요약 정보(title/og/본문 2000자)"만
돌려주는데, 상세 수집은 전체 HTML이 필요하다(네이버 preload·JSON-LD·이미지 갤러리·Cafe24
테이블을 파싱해야 하므로). 그래서 fetch 함수를 따로 두되, 안전판(도메인 게이팅·브라우저
전용 호스트·차단 마커·UA)은 resolve_links/config·browser의 것을 그대로 재사용한다.

반환 rec: {'html', 'final_url', 'status', 'via'('http'|'browser'), 'error', 'gone'}
  - gone: (사유 문자열) 페이지가 영구 소멸(404/삭제된 상품)이라 재시도 무의미 → detail_status='gone'
  - error: 일시 실패(네트워크/차단/타임아웃) → detail_status='error', 다음 실행에서 재시도
"""
import os
import re

import requests
from bs4 import BeautifulSoup

from gonggu.resolve_links.browser import domain_gate
from gonggu.resolve_links.config import (BLOCKED_STATUS_CODES, BLOCKED_TEXT_MARKERS,
                                          BROWSER_ONLY_HOSTS, HTTP_FETCH_TIMEOUT, UA)
from gonggu.resolve_links.urlutil import host_of

from .config import GONE_HTTP_STATUS, GONE_TEXT_MARKERS

_session = requests.Session()
_adapter = requests.adapters.HTTPAdapter(pool_connections=32, pool_maxsize=64)
_session.mount('https://', _adapter)
_session.mount('http://', _adapter)

# JS로 본문 전체를 그리는 껍데기 HTML 감지용 — 이보다 짧으면 브라우저로 넘긴다.
_MIN_HTML = 3000

# Next.js류 SPA 마커. 이런 페이지는 정적 HTML이 커 보여도(수백 KB) 가격이 하이드레이션
# 전 플레이스홀더('00,000원')로 마스킹된 경우가 있다(실측: vyneherb.co.kr, 2026-08-06 —
# 실제 가격은 JS 실행 후에만 DOM에 그려짐). 그 신호가 보이면 브라우저로 넘긴다.
_SPA_MARKERS = ('self.__next_f', '__NEXT_DATA__')
_MASKED_PRICE = '00,000원'


def _needs_hydration(html):
    return any(m in html for m in _SPA_MARKERS) and _MASKED_PRICE in html


def _is_browser_only(url):
    host = host_of(url)
    return any(host == h or host.endswith('.' + h) for h in BROWSER_ONLY_HOSTS)


def _gone_reason(status, html):
    """영구 소멸이 명백할 때만 사유 문자열, 아니면 None. 마커는 앞부분(타이틀/안내 문구가
    있는 영역)만 본다 — 본문 깊숙한 리뷰/댓글에 우연히 같은 문구가 있어도 오탐하지 않게."""
    if status in GONE_HTTP_STATUS:
        return f'HTTP {status}'
    head = (html or '')[:8000]
    for m in GONE_TEXT_MARKERS:
        if m in head:
            return f'페이지 문구: {m}'
    return None


def _blocked_text(html):
    head = (html or '')[:8000].lower()
    return any(m.lower() in head for m in BLOCKED_TEXT_MARKERS)


def _http_fetch(url):
    """requests로 전체 HTML. 브라우저가 필요하면 None(호출부가 폴백)."""
    if _is_browser_only(url):
        return None
    headers = {'User-Agent': UA, 'Accept-Language': 'ko-KR,ko;q=0.9,en;q=0.8',
               'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'}
    try:
        r = _session.get(url, headers=headers, timeout=HTTP_FETCH_TIMEOUT, allow_redirects=True)
    except Exception:
        return None  # 네트워크 실패 — 브라우저 경로가 성공할 수도 있으니 확정하지 않음
    # euc-kr 잔존 페이지 대응: bs4가 meta charset까지 보고 디코딩하게 원본 바이트로 파싱
    # (httpfetch.py의 실측 근거와 동일).
    html = str(BeautifulSoup(r.content, 'lxml')) if r.content else ''
    gone = _gone_reason(r.status_code, html)
    if gone:  # 404/삭제 페이지는 그 자체가 확정 정보 — 브라우저로 다시 열 필요 없음
        return {'html': html, 'final_url': str(r.url), 'status': r.status_code,
                'via': 'http', 'error': None, 'gone': gone}
    if r.status_code in BLOCKED_STATUS_CODES or r.status_code >= 400:
        return None
    if 'html' not in (r.headers.get('Content-Type') or 'text/html').lower():
        return None
    if len(html) < _MIN_HTML or _blocked_text(html):
        return None
    if _needs_hydration(html):  # SPA 마스킹 가격 — JS 실행된 DOM이 필요
        return None
    return {'html': html, 'final_url': str(r.url), 'status': r.status_code,
            'via': 'http', 'error': None, 'gone': None}


def _goto_and_read(page, url, referer=None):
    """1회 시도 — 이동 + 스크롤(지연로딩 이미지의 실제 src 승격, gonggu_scraper 원리) + 판정."""
    rec = {'html': '', 'final_url': None, 'status': None, 'via': 'browser',
           'error': None, 'gone': None}
    try:
        goto_kwargs = {'wait_until': 'domcontentloaded', 'timeout': 25000}
        if referer:
            goto_kwargs['referer'] = referer
        resp = page.goto(url, **goto_kwargs)
        try:
            page.wait_for_load_state('networkidle', timeout=6000)
        except Exception:
            pass
        rec['status'] = resp.status if resp else None
        try:
            for _ in range(6):
                page.evaluate('window.scrollBy(0, document.body.scrollHeight / 6)')
                page.wait_for_timeout(300)
            page.evaluate('window.scrollTo(0, 0)')
            page.wait_for_timeout(300)
        except Exception:
            pass  # 스크롤 실패는 치명적이지 않음 — 이미지 일부가 덜 잡힐 뿐
        rec['final_url'] = page.url
        rec['html'] = page.content()
        rec['gone'] = _gone_reason(rec['status'], rec['html'])
        if rec['gone']:
            return rec
        if rec['status'] in BLOCKED_STATUS_CODES or _blocked_text(rec['html']):
            rec['error'] = f'차단 추정 (HTTP {rec["status"]})'
    except Exception as e:
        rec['error'] = str(e)[:160]
    return rec


def _browser_fetch(page, url):
    """Playwright(LazyPage)로 전체 HTML. 네이버 계열이 첫 시도에서 429로 막히면 네이버
    홈/쇼핑을 먼저 경유해 쿠키를 얹고(사람처럼 진입 — gonggu_scraper의 검증된 워밍업 원리)
    쇼핑검색 referer로 1회만 재시도한다. 그래도 막히면 error로 남겨 다음 실행에서 재시도."""
    rec = _goto_and_read(page, url)
    naver = 'naver.' in host_of(url)
    if naver and rec['error'] and '차단' in rec['error']:
        try:
            page.goto('https://www.naver.com', wait_until='domcontentloaded', timeout=20000)
            page.wait_for_timeout(1200)
            page.goto('https://search.shopping.naver.com/', wait_until='domcontentloaded',
                      timeout=20000)
            page.wait_for_timeout(800)
        except Exception:
            pass  # 워밍업 실패해도 본 재시도는 한다
        retry = _goto_and_read(page, url, referer='https://search.shopping.naver.com/')
        if retry['error']:
            retry['error'] += ' (네이버 워밍업 재시도 후에도 차단 — 세션 만료면 login_naver 재실행)'
        return retry
    return rec


# uc 엔진 활성화(DETAIL_NAVER_ENGINE=uc, 하위호환 이름 유지)와 대상 호스트 목록.
# 기본은 네이버만 — 오픈마켓(G마켓/옥션/쿠팡)이 Playwright에 403을 주면 이들을 uc로 보내
# 뚫을 수 있다(개발자 방식). 예: DETAIL_UC_HOSTS=naver.,gmarket.co.kr,auction.co.kr,coupang.com
def _uc_enabled():
    return os.environ.get('DETAIL_NAVER_ENGINE', '') == 'uc'


def _is_uc_host(url):
    hosts = [h for h in os.environ.get('DETAIL_UC_HOSTS', 'naver.').split(',') if h]
    h = host_of(url)
    return any(k in h for k in hosts)


def _uc_fetch(url):
    """uc 엔진 경로(설치된 진짜 크롬 + 전용 프로필 — naver_uc.py 배경 주석 참고)."""
    from . import naver_uc
    rec = {'html': '', 'final_url': None, 'status': None, 'via': 'uc',
           'error': None, 'gone': None}
    try:
        rec['final_url'], rec['html'] = naver_uc.fetch_sync(url)
        rec['status'] = 200  # selenium은 상태코드를 안 주므로 본문으로 판별
        rec['gone'] = _gone_reason(None, rec['html'])
        if rec['gone']:
            return rec
        # 챌린지 화면(로그인월/캡차/과부하)을 그대로 들고 오면 반드시 error로 — 조용히
        # 통과시키면 NULL투성이 행이 done으로 적재된다(실측 사고 방지, 2026-08-06).
        if ('nid.naver.com' in (rec['final_url'] or '')
                or naver_uc.looks_challenged((rec['html'] or '')[:8000])
                or _blocked_text(rec['html'])):
            rec['error'] = '차단/로그인월/캡차 — uc 창에서 통과 못 함'
    except Exception as e:
        rec['error'] = f'uc 실패: {str(e)[:140]}'
    return rec


def fetch_detail_page(page, url):
    """진입점 — domain_gate로 감싸 같은 도메인 동시 접근을 MAX_PER_DOMAIN으로 제한한다
    (resolve_links와 같은 세마포어를 공유하므로 두 단계가 겹쳐 돌아도 상한은 하나다)."""
    if not re.match(r'^https?://', url or ''):
        return {'html': '', 'final_url': url, 'status': None, 'via': 'http',
                'error': f'URL 형식 아님: {str(url)[:80]}', 'gone': None}
    with domain_gate(url):
        if _uc_enabled() and _is_uc_host(url):
            return _uc_fetch(url)
        rec = _http_fetch(url)
        if rec is not None:
            return rec
        return _browser_fetch(page, url)
