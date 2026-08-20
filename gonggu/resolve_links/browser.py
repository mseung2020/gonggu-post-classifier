"""Playwright 페이지 조작/파싱 원시 함수 — 판단(LLM) 없이 "페이지를 열어서 뭐가 있는지 본다"만 담당."""
import os
import re
import threading
import time
from contextlib import contextmanager

from playwright_stealth import Stealth

from .antibot import is_linkbio_hub, is_uc_host
from .config import (AUTH_STATE_FILE, BLOCKED_STATUS_CODES, BLOCKED_TEXT_MARKERS, MAX_BROWSERS,
                     MAX_PER_DOMAIN, SLOW_REDIRECT_DOMAINS, UA)
from .httpfetch import extract_jsonld, rec_from_html, try_http_fetch
from .urlutil import host_of

# fast(무인) resolve/rescan가 uc 호스트를 만나 브라우저를 생략할 때 남기는 노트 — 반드시
# '재검증 중 차단'을 포함해야 reverify_uc(LIKE '%재검증 중 차단%')가 2단 uc 대상으로 주워간다.
UC_SKIP_NOTE = '재검증 중 차단(네이버/오픈마켓 로그인월 호스트) — fast에서 브라우저 생략, uc 패스 대상'

# ── 후보 하나가 실제로 어느 경로로 처리됐는지 세는 카운터(2026-08-18, 속도 개선 공사 A단계).
# httpfetch.stats()는 "requests 패스트패스 자체를 시도한 것 중 몇 번 적중했는지"만 보여주는데,
# 실제로 브라우저를 몇 번이나 띄웠는지(=RESOLVE_CONCURRENCY:MAX_BROWSERS 비율을 재는 근거,
# 아이디어 C)는 그 앞뒤(링크인바이오 구조화로 아예 페치가 필요 없던 경우, uc 호스트라 페치 자체를
# 생략한 경우)까지 합쳐야 알 수 있다. core.py가 각 분기에서 bump_via를 호출하고, fetch()도
# 실제로 쓴 경로(via='http'/'browser'/'uc')를 여기서 직접 센다 — 한 곳(_via_lock)에 모아야
# 두 파일이 각자 세다 이중집계/누락이 안 생긴다. 단위는 "상품 1건"이 아니라 "후보 URL 페치
# 시도 1건"이다(finalize_pick 안에서 후보 하나가 한 번 더 fetch를 부를 수 있음) — runner.py의
# 출력 문구도 그렇게 명시한다.
_via_lock = threading.Lock()
_via_stats = {}


def bump_via(via):
    with _via_lock:
        _via_stats[via] = _via_stats.get(via, 0) + 1


def via_stats():
    with _via_lock:
        return dict(_via_stats)


# ── 허가증 점유 계측(2026-08-19) ────────────────────────────────────────────
# 브라우저 허가증(MAX_BROWSERS개)은 LazyPage가 브라우저를 띄울 때 잡고 _teardown에서 놓는다.
# 그 사이에는 LLM#2/#3 호출도 들어가는데(한 상품이 브라우저→LLM→브라우저→LLM을 오간다) 그동안
# 브라우저는 아무 일도 안 하면서 14개뿐인 허가증 하나를 계속 점유한다.
#
# "그러면 LLM 호출 전에 허가증을 놓으면 되지 않나"가 자연스러운 발상인데, 허가증은 곧 살아있는
# 크롬 프로세스 수라 놓으려면 브라우저를 닫아야 하고(안 닫고 놓으면 MAX_BROWSERS를 넘겨 뜬다 =
# 2026-07-30 스왑 사고), 다시 띄우는 데 3.9초가 든다. 즉 이득이 나려면 "LLM 대기가 3.9초보다
# 충분히 길고, 그 순간 실제로 기다리는 워커가 있어야" 한다 — 감으로 정할 문제가 아니다.
# (관련 실측이 이미 있다: LazyPage.release_if_contended 주석 — 무조건 넘기기는 1.8배 느렸다.)
#
# 그래서 고치기 전에 재기부터 한다. held는 허가증을 쥐고 있던 총 시간, busy는 그중 실제로
# 브라우저가 페이지를 여느라 쓴 시간. busy/held가 낮을수록 "허가증을 놀리고 있다"는 뜻이고,
# 그 차이가 클 때만 구조 변경(크롤 단계와 LLM 단계 분리)이 값을 한다.
_permit_lock = threading.Lock()
_permit_stats = {'held_sec': 0.0, 'busy_sec': 0.0, 'sessions': 0,
                 'wait_sec': 0.0, 'waits': 0}
# 지금 허가증을 쥐고 있는 워커들의 획득 시각 {소유자 id: monotonic}. ⚠ 이게 없으면 held가
# "닫힌 브라우저"만 세는데(_teardown에서만 누적) busy는 살아있는 워커 것까지 다 세서 분모만
# 빠진다 — 실제로 첫 실측(2026-08-20)에서 "총 449초 중 작업 1202초, 유휴 -168%"라는 음수가
# 나왔다. 리포트 시점에 아직 안 닫힌 세션의 경과 시간까지 더해야 비율이 성립한다.
_permit_open = {}


def _permit_acquired(owner):
    with _permit_lock:
        _permit_open[id(owner)] = time.monotonic()


def _permit_released(owner):
    with _permit_lock:
        started = _permit_open.pop(id(owner), None)
        if started is not None:
            _permit_stats['held_sec'] += time.monotonic() - started
            _permit_stats['sessions'] += 1


def _add_permit_busy(seconds):
    with _permit_lock:
        _permit_stats['busy_sec'] += seconds


def _add_permit_wait(seconds):
    """허가증을 기다린 시간. 경합이 없으면 0에 수렴한다(세마포어가 즉시 통과)."""
    if seconds <= 0.001:      # 즉시 통과 = 경합 없음. 잡음으로 waits를 부풀리지 않는다.
        return
    with _permit_lock:
        _permit_stats['wait_sec'] += seconds
        _permit_stats['waits'] += 1


def permit_stats():
    """{held_sec, busy_sec, sessions, open_sessions, wait_sec, waits, idle_ratio} —
    idle_ratio는 허가증을 쥔 채 브라우저를 안 쓴 시간의 비율. 아직 안 닫힌 세션의 경과 시간도
    held에 포함한다(위 주석). 표본이 없으면 idle_ratio=None.

    ⚠ idle_ratio 하나로 판단하지 말 것 — 허가증이 놀아도 기다리는 워커가 없으면 손해가 0이다.
    wait_sec(실제로 줄 선 시간)과 같이 봐야 "구조를 바꿔서 회수할 수 있는 양"이 나온다."""
    with _permit_lock:
        s = dict(_permit_stats)
        now = time.monotonic()
        s['open_sessions'] = len(_permit_open)
        s['held_sec'] += sum(now - t for t in _permit_open.values())
        s['sessions'] += len(_permit_open)
    s['idle_ratio'] = (1 - s['busy_sec'] / s['held_sec']) if s['held_sec'] > 0 else None
    return s


# ── 브라우저 없는 빠른 패스(Tier0) 스위치(2026-08-18, 속도개선 공사 F단계) ──
# runner.py가 한 프로세스 안에서 두 패스를 순차로(동시 아님) 돌린다: 먼저 이 스위치를 꺼서
# 브라우저가 필요한 후보를 전부 'needs_browser'로 보류시키는 빠른 패스(링크인바이오 구조화/
# uc_host_skip/http 패스트패스만, 실측상 후보의 약 80%), 그 다음 스위치를 켜서 보류된 나머지만
# 기존 경로(MAX_BROWSERS 풀)로 재시도한다. 두 패스가 같은 프로세스에서 순차로 도니 전역 변수
# 하나로 충분하다(동시에 두 값이 필요한 상황이 없음).
_allow_browser = True


def set_allow_browser(allow):
    global _allow_browser
    _allow_browser = allow


def browser_allowed():
    return _allow_browser


# fetch()/fetch_with_browser()가 브라우저 차례인데 _allow_browser=False라 실제로 열지 않고
# 돌려주는 공용 rec — status/error 둘 다 None이라 BLOCKED_STATUS_CODES/BLOCKED_TEXT_MARKERS
# 검사도 자연히 통과(차단 아님)해서 uc 옵트인 분기도 안 걸린다. 호출부(core.py/picker.py)가
# via=='needs_browser'를 보고 이 후보(또는 상품 전체)를 Tier1로 넘긴다.
def _needs_browser_rec():
    return {'status': None, 'final_url': None, 'title': None, 'og_image': None, 'jsonld': {},
            'body_text': '', 'error': None, 'via': 'needs_browser'}


def fast_skip_uc_host(url):
    """이 URL을 fast(무인) 경로에서 브라우저로 열지 말고 uc 패스(reverify_uc)로 넘길지 판단.
    RESOLVE_UC=1(reverify_uc 2단 패스)이면 False라 그 패스에선 실제로 uc로 연다. 환경변수를
    호출 시점에 읽어(import 시점 아님) 런타임 토글이 반영되게 한다(browser._uc_enabled_for와 동일 규약)."""
    if os.environ.get('RESOLVE_UC', '0') == '1':
        return False
    return is_uc_host(url)

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
        started = time.monotonic()
        try:
            self._sem.acquire()
        finally:
            # 허가증을 못 받아 실제로 줄 서 있던 시간(2026-08-20 추가). 유휴율만으로는 판단이
            # 안 된다 — 허가증이 놀아도 기다리는 워커가 없으면 손해가 0이기 때문이다.
            # "유휴 65% + 대기 0초"면 지금 구조로 충분하고, "유휴 65% + 대기 수천 초"면 크롤/LLM
            # 단계 분리가 그만큼을 회수한다는 뜻이다(permit_stats 주석 참고).
            _add_permit_wait(time.monotonic() - started)
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


# ── 호스트별 적응형 쿨다운(2026-08-11) — 429/403을 준 호스트만 잠깐 쉬어 레이트리밋 폭발을 막는다.
# 어떤 호스트가 차단으로 응답하면 그 호스트로 가는 다음 요청들을 HOST_COOLDOWN_SEC초 뒤로 미룬다
# (전역 도메인이 아니라 그 호스트만 — 정상 호스트는 영향 없음). uc를 기본 tier로 돌리기 시작하면서
# 네이버/오픈마켓을 두들겨 IP가 눌리는 걸 예방하는 안전망. 정상 응답이 오면 쿨다운은 자연 만료된다.
_host_cooldown = {}
_host_cooldown_lock = threading.Lock()
_HOST_COOLDOWN_SEC = float(os.environ.get('HOST_COOLDOWN_SEC', '20'))


def _cooldown_wait(host):
    if not host or _HOST_COOLDOWN_SEC <= 0:
        return
    with _host_cooldown_lock:
        until = _host_cooldown.get(host, 0)
    remaining = until - time.time()
    if remaining > 0:
        time.sleep(min(remaining, _HOST_COOLDOWN_SEC))


def _mark_blocked(host):
    if not host or _HOST_COOLDOWN_SEC <= 0:
        return
    with _host_cooldown_lock:
        _host_cooldown[host] = time.time() + _HOST_COOLDOWN_SEC


def _uc_enabled_for(url):
    """uc 옵트인 폴백 대상인지 — RESOLVE_UC=1이고 호스트가 RESOLVE_UC_HOSTS(기본 naver.)에
    걸릴 때만. 환경변수를 호출 시점에 읽어서(import 시점 아님) reverify_uc가 런타임에 켜도
    반영되게 한다. 기본은 꺼짐 → 대량·무인 resolve 본 경로는 완전히 그대로(골든 무풍)."""
    if os.environ.get('RESOLVE_UC', '0') != '1':
        return False
    hosts = [h for h in os.environ.get('RESOLVE_UC_HOSTS', 'naver.').split(',') if h]
    h = host_of(url)
    return any(k in h for k in hosts)


def _looks_blocked(rec):
    """재검증 페이지가 로그인월/캡차/봇차단으로 막힌 낌새인지 — picker의 차단 판정과 같은 기준
    (BLOCKED_STATUS_CODES / BLOCKED_TEXT_MARKERS)에 네이버 로그인 리다이렉트(nid.naver.com)를 더한다."""
    if rec.get('status') in BLOCKED_STATUS_CODES:
        return True
    if 'nid.naver.com' in (rec.get('final_url') or ''):
        return True
    bt = (rec.get('body_text') or '').lower()
    return any(m.lower() in bt for m in BLOCKED_TEXT_MARKERS)


def _uc_fetch(url):
    """uc 엔진(gonggu.uc_engine)으로 재시도해 browser.fetch()와 같은 모양의 rec를 만든다.
    실패하거나 여전히 로그인월/캡차면 error를 담은 rec를 돌려준다(호출부가 원래 차단 rec를
    유지하도록 — _uc_fetch 성공 시에만 교체). 진행 상황을 stdout에 남긴다 — uc 경로는 사람이
    지켜보는 2단 패스에서만 켜지므로 "지금 uc가 실제로 뭘 하고 있나"가 보여야 디버깅이 된다."""
    print(f'    · uc 재시도: {url[:100]}', flush=True)
    try:
        from gonggu.uc_engine import fetch_sync, looks_challenged
        final_url, html = fetch_sync(url)
    except Exception as e:
        print(f'      ✗ uc 엔진 실패: {str(e)[:140]}', flush=True)
        return {'status': None, 'final_url': None, 'title': None, 'og_image': None,
                'jsonld': {}, 'body_text': '', 'error': f'uc 실패: {str(e)[:140]}', 'via': 'uc'}
    if not html or 'nid.naver.com' in (final_url or '') or looks_challenged(html[:8000]):
        print(f'      ✗ uc 통과 못함(로그인월/캡차/빈응답): {(final_url or "")[:100]}', flush=True)
        return {'status': None, 'final_url': final_url, 'title': None, 'og_image': None,
                'jsonld': {}, 'body_text': '', 'error': 'uc 차단/로그인월/캡차 통과 못함', 'via': 'uc'}
    print(f'      ✓ uc 통과: {(final_url or "")[:100]} (html {len(html)}자)', flush=True)
    return rec_from_html(html, final_url, via='uc')


def fetch(page, url, wait_extra=1.5, referer=None):
    """판별에 필요한 정보를 얻는 기본 진입점 — requests로 충분하면 그걸로 끝내고(0.1~0.3초),
    모자라거나 차단되면 브라우저로 넘어간다(3~4초). rec['via']로 어느 쪽이었는지 알 수 있다.

    ⚠ requests 경로를 탄 경우 page는 아무 데도 이동하지 않은 상태다 — 그 뒤에 DOM이
    필요하면(extract_collection_links 등) 반드시 fetch_with_browser()로 다시 열어야 한다.
    core.py의 링크모음/스토어메인 분기 참고.

    uc 옵트인 폴백(2026-08-07): 위 경로가 로그인월/캡차로 막혔고 RESOLVE_UC=1이면
    undetected_chromedriver로 한 번 더 열어본다(reverify_uc 2단 패스 전용). uc가 통과하면 그
    결과로 교체, 못 뚫으면 원래 차단 rec를 그대로 둔다. 기본 OFF → 본 경로 무변경.

    ⚠ 대상 호스트 판정은 url뿐 아니라 **최종 도착지(final_url)**로도 한다 — 링크인바이오
    버튼은 chosen_href가 인포크(link.inpock…)라서 호스트만 보면 네이버가 아니지만, 실제 차단은
    그게 리다이렉트되는 네이버 페이지에서 난다(2026-08-07 첫 실행에서 uc가 아예 안 걸린 원인:
    인포크 호스트만 보고 스킵했음). 그리고 네이버 최종 상품 URL이 깔끔하면 그 URL을 직접 열고,
    로그인 리다이렉트(nid.naver.com)면 원본을 열어 uc가 쿠키 실은 채 새로 리다이렉트를 따라가게 한다."""
    _cooldown_wait(host_of(url))  # 이 호스트가 최근 차단으로 쿨다운 중이면 잠깐 기다린다
    with domain_gate(url):
        rec = try_http_fetch(url, referer)
        if rec is None:
            if not _allow_browser:
                bump_via('needs_browser')
                return _needs_browser_rec()
            started = time.monotonic()   # 허가증 점유 계측(permit_stats 참고)
            try:
                rec = _browser_fetch(page, url, wait_extra, referer)
            finally:
                _add_permit_busy(time.monotonic() - started)
    # 도메인 게이트 밖에서 uc 재시도 — uc는 자체 락으로 전역 직렬화하므로 게이트를 겹쳐 잡지 않는다.
    final = rec.get('final_url') or ''
    uc_on = os.environ.get('RESOLVE_UC', '0') == '1'
    blocked = _looks_blocked(rec)
    if blocked:  # 차단한 호스트에 쿨다운 등록 — 다음 요청들이 몰려가 429가 폭발하지 않게
        _mark_blocked(host_of(final) or host_of(url))
    hub = is_linkbio_hub(url)   # 인포크 등 허브 URL(resolved 실패로 href가 허브로 남은 경우) — 네이버로 리다이렉트됨
    if uc_on and blocked:
        # 진단(RESOLVE_UC일 때만) — uc가 왜 걸렸/안 걸렸는지 근거를 그대로 보여준다.
        print(f'    · 차단감지 url={host_of(url)} final={host_of(final)} status={rec.get("status")} '
              f'err={(rec.get("error") or "")[:40]} ucURL={_uc_enabled_for(url)} '
              f'ucFinal={_uc_enabled_for(final)} hub={hub}', flush=True)
    # uc 발동: 차단 + (url/final이 uc 대상 호스트 OR url이 링크인바이오 허브). 허브면 uc가
    # 리다이렉트를 쿠키 싣고 따라가 네이버 상품에 도달한다(인포크 버튼 케이스 커버).
    if uc_on and blocked and (_uc_enabled_for(url) or _uc_enabled_for(final) or hub):
        target = final if (_uc_enabled_for(final) and 'nid.naver.com' not in final) else url
        uc_rec = _uc_fetch(target)
        if not uc_rec.get('error'):
            bump_via('uc')
            return uc_rec
    bump_via(rec.get('via'))
    return rec


def fetch_with_browser(page, url, wait_extra=1.5, referer=None):
    """requests 패스트패스를 건너뛰고 무조건 브라우저로 연다 — 결과 rec뿐 아니라 "page가 실제로
    그 URL에 가 있는 상태"가 필요할 때 쓴다. _allow_browser=False(Tier0 빠른 패스)면 실제로
    열지 않고 needs_browser rec를 돌려준다 — 호출부가 이 후보(상품)를 Tier1로 넘긴다."""
    if not _allow_browser:
        bump_via('needs_browser')
        return _needs_browser_rec()
    with domain_gate(url):
        # domain_gate 대기는 busy에 안 넣는다 — 그건 브라우저가 일한 시간이 아니라 도메인
        # 몰림 때문에 줄 선 시간이라, 여기 포함하면 "허가증을 알차게 썼다"고 착각하게 된다.
        started = time.monotonic()
        try:
            rec = _browser_fetch(page, url, wait_extra, referer)
        finally:
            _add_permit_busy(time.monotonic() - started)
    bump_via(rec.get('via'))
    return rec


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
    # PW_HEADLESS=0 이면 실제 창을 띄운다(기본은 기존과 동일하게 headless) — 네이버처럼
    # headless 자체를 탐지하는 의심이 있을 때 진단용(2026-08-06, 스마트스토어 429 조사).
    # 대량 실행에서 켜면 창이 수십 개 뜨므로 진단/소량에서만 쓸 것.
    headless = os.environ.get('PW_HEADLESS', '1') != '0'
    browser = pw.chromium.launch(headless=headless, args=[
        '--disable-blink-features=AutomationControlled',
        '--disable-gpu',  # 스크린샷/렌더링 결과가 필요 없는 스크래핑이라 브라우저마다 따로 뜨는
                          # GPU 프로세스가 순수 낭비다(실측 확인, 2026-07-30 — ps에 브라우저 수만큼
                          # --type=gpu-process가 떠 있었음).
    ])
    ctx_kwargs = dict(user_agent=UA, locale='ko-KR', viewport={'width': 1360, 'height': 900},
                       extra_http_headers={'Accept-Language': 'ko-KR,ko;q=0.9,en;q=0.8'})
    # PW_USE_AUTH=0 이면 저장된 네이버 로그인 세션을 싣지 않는다(익명 크롤링).
    # 배경(2026-08-06 실측): 로그인 세션을 실은 접근만 스마트스토어가 429로 차단하는
    # "계정 플래그" 상태가 확인됨 — 익명은 같은 IP·같은 브라우저로도 정상 응답. 이런 때
    # 세션이 오히려 독이므로 끌 수 있게 한다. 기본은 기존과 동일(세션 사용).
    if AUTH_STATE_FILE.exists() and os.environ.get('PW_USE_AUTH', '1') != '0':
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
            _permit_acquired(self)      # 허가증 점유 계측(permit_stats 참고)
            try:
                self._browser, self._ctx, self._page = new_context_page(self._pw)
            except Exception:
                _permit_released(self)
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
        _permit_released(self)
        _browser_permits.release()
