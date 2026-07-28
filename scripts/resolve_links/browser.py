"""Playwright 페이지 조작/파싱 원시 함수 — 판단(LLM) 없이 "페이지를 열어서 뭐가 있는지 본다"만 담당."""
import json
import re
import threading
import time
from contextlib import contextmanager

from playwright_stealth import Stealth

from .config import AUTH_STATE_FILE, MAX_PER_DOMAIN, SLOW_REDIRECT_DOMAINS, UA
from .urlutil import host_of

# 도메인당 동시 접근 상한을 "어느 상품이 이 도메인을 먼저 후보로 들고 있었나"가 아니라
# "지금 실제로 이 도메인에 Playwright 네비게이션을 여는 순간"에 건다 — 예전엔 runner.py가
# 상품의 첫 후보 URL(예: 링크인바이오 허브)을 기준으로 워커를 도메인 락했는데, 정작 무거운
# 브라우저 접근은 LLM#2가 고른 최종 목적지(전혀 다른 도메인)에서 일어나서 보호 대상이
# 어긋나 있었다(실측 확인, 2026-07-27 — 상품의 91.8%가 인포크를 첫 후보로 공유하는데
# 인포크 자체는 requests 기반 캐시된 호출이라 안 무거움). fetch() 호출 지점 자체를
# 게이팅하면 실제 목적지가 뭐든 항상 정확히 보호된다.
_domain_semaphores = {}
_domain_semaphores_lock = threading.Lock()


def _domain_semaphore(domain):
    with _domain_semaphores_lock:
        sem = _domain_semaphores.get(domain)
        if sem is None:
            sem = threading.Semaphore(MAX_PER_DOMAIN)
            _domain_semaphores[domain] = sem
        return sem


@contextmanager
def domain_gate(url):
    """실제로 page.goto()를 부르는 모든 지점(fetch/follow_redirect)이 이걸로 감싼다 — url의
    호스트별로 MAX_PER_DOMAIN을 넘는 동시 네비게이션을 막는다."""
    domain = host_of(url)
    sem = _domain_semaphore(domain) if domain else None
    if sem:
        sem.acquire()
    try:
        yield
    finally:
        if sem:
            sem.release()


def meta(page, prop):
    try:
        el = page.query_selector(f'meta[property="{prop}"]') or page.query_selector(f'meta[name="{prop}"]')
        return el.get_attribute('content') if el else None
    except Exception:
        return None


def extract_jsonld(html):
    out = {}
    for m in re.finditer(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, re.S):
        try:
            data = json.loads(m.group(1).strip())
        except Exception:
            continue
        items = data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])
        if isinstance(data, dict) and '@graph' in data:
            items = data['@graph']
        for it in items:
            if isinstance(it, dict):
                t = it.get('@type', '')
                t = t if isinstance(t, str) else ','.join(t)
                if 'Product' in t:
                    img = it.get('image')
                    offers = it.get('offers') or {}
                    if isinstance(offers, list):
                        offers = offers[0] if offers else {}
                    return {'name': it.get('name'), 'image': img[0] if isinstance(img, list) else img,
                            'price': offers.get('price'), 'currency': offers.get('priceCurrency')}
    return out


def _extract_once(page):
    title = meta(page, 'og:title') or (page.title() or '').strip()
    html = page.content()
    og_image = meta(page, 'og:image')
    jsonld = extract_jsonld(html)
    try:
        # 가격·구성이 JSON-LD가 아니라 본문 텍스트 중간에 있는 경우가 많아(예: "정가 238,000 공구가
        # 166,600") 2000자로 넉넉히 잡아서 LLM#3 판별 근거로 삼는다.
        body_text = page.inner_text('body')[:2000].replace('\n', ' ')
    except Exception:
        body_text = ''
    return title, og_image, jsonld, body_text


def fetch(page, url, wait_extra=1.5, referer=None):
    rec = {'status': None, 'final_url': None, 'title': None, 'og_image': None, 'jsonld': {},
           'body_text': '', 'error': None}
    with domain_gate(url):
        try:
            goto_kwargs = {'wait_until': 'domcontentloaded', 'timeout': 25000}
            if referer:
                goto_kwargs['referer'] = referer
            resp = page.goto(url, **goto_kwargs)
            try:
                page.wait_for_load_state('networkidle', timeout=6000)
            except Exception:
                pass
            time.sleep(wait_extra)
            rec['status'] = resp.status if resp else None
            rec['final_url'] = page.url

            # 네이버 마케팅 단축링크류는 클라이언트 사이드 리다이렉트가 늦게 끝나는 경우가 있음
            if host_of(rec['final_url']) in SLOW_REDIRECT_DOMAINS:
                time.sleep(3)
                try:
                    page.wait_for_load_state('networkidle', timeout=4000)
                except Exception:
                    pass
                rec['final_url'] = page.url

            # blog.naver.com(PC)은 본문이 iframe 안에 있어 본문 텍스트/링크 추출이 전부 0으로
            # 나옴 — 모바일(m.blog.naver.com)은 iframe 없이 직접 렌더링하니 도착지가 PC
            # 블로그면 다시 이동.
            if host_of(rec['final_url']) == 'blog.naver.com':
                mobile_url = re.sub(r'^https?://blog\.naver\.com/', 'https://m.blog.naver.com/', rec['final_url'])
                page.goto(mobile_url, wait_until='domcontentloaded', timeout=25000)
                try:
                    page.wait_for_load_state('networkidle', timeout=6000)
                except Exception:
                    pass
                time.sleep(wait_extra)
                rec['final_url'] = page.url

            title, og_image, jsonld, body_text = _extract_once(page)
            if not jsonld.get('image') and not og_image:
                time.sleep(2)
                title, og_image, jsonld, body_text = _extract_once(page)

            rec['title'], rec['og_image'], rec['jsonld'], rec['body_text'] = title, og_image, jsonld, body_text
        except Exception as e:
            rec['error'] = str(e)[:160]
    return rec


def new_context_page(pw):
    browser = pw.chromium.launch(headless=True, args=['--disable-blink-features=AutomationControlled'])
    ctx_kwargs = dict(user_agent=UA, locale='ko-KR', viewport={'width': 1360, 'height': 900},
                       extra_http_headers={'Accept-Language': 'ko-KR,ko;q=0.9,en;q=0.8'})
    if AUTH_STATE_FILE.exists():
        ctx_kwargs['storage_state'] = str(AUTH_STATE_FILE)
    ctx = browser.new_context(**ctx_kwargs)
    # 기본값이 Win32/en-US라 UA(Mac)·locale(ko-KR)이랑 안 맞으면 오히려 더 튀어서 맞춰준다.
    Stealth(navigator_platform_override='MacIntel',
            navigator_languages_override=('ko-KR', 'ko')).apply_stealth_sync(ctx)
    return browser, ctx, ctx.new_page()
