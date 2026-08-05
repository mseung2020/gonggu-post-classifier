"""Playwright 페이지 조작/파싱 원시 함수 — 판단(LLM) 없이 "페이지를 열어서 뭐가 있는지 본다"만 담당."""
import re
import threading
import time
from contextlib import contextmanager

from playwright_stealth import Stealth

from .config import AUTH_STATE_FILE, MAX_BROWSERS, MAX_PER_DOMAIN, SLOW_REDIRECT_DOMAINS, UA
from .httpfetch import extract_jsonld, try_http_fetch
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

class _BrowserPermits:
    """실제로 뜬 Playwright 브라우저 프로세스의 동시 개수 상한(LazyPage 참고) — 워커 스레드
    수와 별개로, 무거운 브라우저 프로세스 자체는 하드웨어가 감당할 만큼만 동시에 살아있게 한다.

    맨 Semaphore가 아니라 이 래퍼를 쓰는 이유는 "지금 허가증을 기다리는 워커가 있는가"를 알아야
    하기 때문이다. 워커는 브라우저를 한 번 띄우면 다음 상품에서도 재사용하려고 계속 들고 있는데
    (재기동에 3.9초가 들어서), 브라우저를 더 안 쓰게 된 뒤에도 계속 들고 있으면 대기자는 큐가
    다 빌 때까지 묶인다 — 그 워커는 그동안 패스트패스로 끝날 값싼 건조차 처리하지 못한다.
    contended를 보고 놓아주면 한산할 땐 재사용해서 빠르고 붐빌 땐 노는 워커가 없다.

    ⚠ 브라우저 작업의 처리량 자체는 어차피 MAX_BROWSERS가 상한이라, 이걸 고친다고 크게
    빨라지진 않는다(실측, 2026-08-01 — 브라우저 필요 비율 30%에서 1.3배, 60% 이상에선 차이
    없음). 진짜 레버는 브라우저 수요를 줄이는 것(httpfetch 패스트패스)이다."""

    def __init__(self, limit):
        self._sem = threading.Semaphore(limit)
        self._lock = threading.Lock()
        self._waiting = 0

    def acquire(self):
        with self._lock:
            self._waiting += 1
        try:
            self._sem.acquire()
        finally:
            with self._lock:
                self._waiting -= 1

    def release(self):
        self._sem.release()

    @property
    def contended(self):
        with self._lock:
            return self._waiting > 0


_browser_permits = _BrowserPermits(MAX_BROWSERS)


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
    """판별에 필요한 정보를 얻는 기본 진입점 — requests로 충분하면 그걸로 끝내고(0.1~0.3초),
    모자라거나 차단되면 브라우저로 넘어간다(3~4초). rec['via']로 어느 쪽이었는지 알 수 있다.

    ⚠ requests 경로를 탄 경우 page는 아무 데도 이동하지 않은 상태다 — 그 뒤에 DOM이
    필요하면(extract_collection_links 등) 반드시 fetch_with_browser()로 다시 열어야 한다.
    core.py의 링크모음/스토어메인 분기 참고."""
    with domain_gate(url):
        rec = try_http_fetch(url, referer)
        if rec is not None:
            return rec
        return _browser_fetch(page, url, wait_extra, referer)


def fetch_with_browser(page, url, wait_extra=1.5, referer=None):
    """requests 패스트패스를 건너뛰고 무조건 브라우저로 연다 — 결과 rec뿐 아니라 "page가 실제로
    그 URL에 가 있는 상태"가 필요할 때 쓴다."""
    with domain_gate(url):
        return _browser_fetch(page, url, wait_extra, referer)


def _browser_fetch(page, url, wait_extra, referer):
    rec = {'status': None, 'final_url': None, 'title': None, 'og_image': None, 'jsonld': {},
           'body_text': '', 'error': None, 'via': 'browser'}
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
    browser = pw.chromium.launch(headless=True, args=[
        '--disable-blink-features=AutomationControlled',
        '--disable-gpu',  # 스크린샷/렌더링 결과가 필요 없는 스크래핑이라 브라우저마다 따로 뜨는
                          # GPU 프로세스가 순수 낭비다(실측 확인, 2026-07-30 — ps에 브라우저 수만큼
                          # --type=gpu-process가 떠 있었음).
    ])
    ctx_kwargs = dict(user_agent=UA, locale='ko-KR', viewport={'width': 1360, 'height': 900},
                       extra_http_headers={'Accept-Language': 'ko-KR,ko;q=0.9,en;q=0.8'})
    if AUTH_STATE_FILE.exists():
        ctx_kwargs['storage_state'] = str(AUTH_STATE_FILE)
    ctx = browser.new_context(**ctx_kwargs)
    # 기본값이 Win32/en-US라 UA(Mac)·locale(ko-KR)이랑 안 맞으면 오히려 더 튀어서 맞춰준다.
    Stealth(navigator_platform_override='MacIntel',
            navigator_languages_override=('ko-KR', 'ko')).apply_stealth_sync(ctx)
    page = ctx.new_page()
    # 판단에 쓰는 건 title/og:image URL 문자열/JSON-LD/본문 텍스트뿐, 이미지·폰트·미디어를
    # 실제로 렌더링해서 보는 게 아니므로 아예 안 받는다 — 페이지당 대역폭·메모리·로드 시간을
    # 크게 줄인다.
    page.route('**/*', lambda route: route.abort()
               if route.request.resource_type in ('image', 'font', 'media') else route.continue_())
    return browser, ctx, page


class LazyPage:
    """워커 스레드 시작 시 곧바로 브라우저를 띄우지 않고, 실제로 page.* 호출이 필요해지는 첫
    순간까지 생성을 미룬다 — 상당수 상품은 links.linkbio_candidates()/httpfetch의 requests
    경로만으로 끝나 브라우저가 아예 필요 없는데, 예전엔 워커마다 무조건 브라우저를 띄워서
    사실상 "워커 수 = 크롬 프로세스 수"였다(실측 확인, 2026-07-30 — 워커 200개에 크롬 관련
    프로세스 550개+, 스왑 32GB 소진으로 시스템 전체가 먹통이 됨).

    생성은 _browser_permits(MAX_BROWSERS)로 동시 개수를 제한하되, 한 번 띄운 브라우저는
    다음 상품에서도 재사용한다(재기동 3.9초를 아끼려고). 다만 허가증을 무한정 쥐고 있으면
    대기자가 굶으므로, 상품 사이사이 release_if_contended()로 "기다리는 워커가 있으면 닫고
    넘겨준다" — runner의 워커 루프가 매 상품마다 호출한다."""

    def __init__(self, pw, save_auth_state=False):
        self._pw = pw
        self._save_auth_state = save_auth_state
        self._browser = self._ctx = self._page = None
        self._used_recently = False

    def _ensure(self):
        self._used_recently = True
        if self._page is None:
            _browser_permits.acquire()
            try:
                self._browser, self._ctx, self._page = new_context_page(self._pw)
            except Exception:
                _browser_permits.release()
                raise
        return self._page

    def __getattr__(self, name):
        # page.url처럼 속성 접근도 있고 page.goto(...)처럼 메서드 호출도 있어서, 실제 Page
        # 객체를 만든 뒤 그 위임 대상에서 이름을 그대로 찾아 돌려준다 — 호출부(core.py 등)는
        # 이게 진짜 Page인지 지연 생성 래퍼인지 신경 쓸 필요가 없다.
        return getattr(self._ensure(), name)

    @property
    def used_since_release(self):
        """마지막 release_if_contended() 이후 이 워커가 실제로 브라우저를 건드렸는지 —
        crawl_pool의 조건부 ITEM_DELAY(4단계 D1)가 "이번 항목에서 브라우저를 안 썼으면
        안티봇 대기를 건너뛴다"를 판단할 때 쓴다(읽기만 하고 리셋하지 않음 —
        리셋은 release_if_contended가 담당)."""
        return self._used_recently

    def release_if_contended(self):
        """상품 하나를 끝낼 때마다 호출 — "대기자가 있는데 나는 방금 이 브라우저를 안 썼다"일
        때만 닫고 허가증을 넘긴다.

        "안 썼을 때만"이 핵심이다. 대기자가 있다고 무조건 넘기면, 브라우저가 계속 필요한
        워커들끼리 허가증을 돌려가며 매번 재기동(3.9초)하느라 오히려 느려진다(실측 확인,
        2026-08-01 — 브라우저 필요 비율 90% 시뮬레이션에서 무조건 넘기기가 1.8배 느림).
        방금 안 썼다면 다음에도 안 쓸 가능성이 크니(패스트패스로 끝나는 구간에 들어선 것)
        붙잡고 있어봐야 낭비고, 방금 썼다면 계속 쥐고 있는 게 이득이다."""
        used, self._used_recently = self._used_recently, False
        if self._page is not None and not used and _browser_permits.contended:
            self._teardown()

    def close(self):
        self._teardown()

    def _teardown(self):
        """브라우저를 아예 안 띄웠으면(패스트패스만 탔으면) 닫을 것도, 허가증을 반납할 것도
        없다. 세션 저장은 닫는 시점마다 해둔다 — release_if_contended로 중간에 닫힐 수 있어서
        "마지막에 한 번"에 의존하면 저장을 통째로 놓친다."""
        if self._page is None:
            return
        try:
            if self._save_auth_state:
                AUTH_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
                self._ctx.storage_state(path=str(AUTH_STATE_FILE))
        except Exception:
            pass
        try:
            self._browser.close()
        except Exception:
            pass
        # 닫기가 실패하더라도 허가증은 반드시 돌려줘야 한다 — 안 그러면 남은 워커 전체가
        # 그만큼 영구히 굶는다.
        self._browser = self._ctx = self._page = None
        _browser_permits.release()
