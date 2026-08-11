"""네이버/오픈마켓 렌더링 엔진 — undetected_chromedriver(uc) + 전용 프로필 (공용 모듈).

배경(2026-08-06): 스마트스토어가 Playwright 브라우저를 세션 있으면 429, 없으면 로그인월로
차단하는 상태가 실측 확인됨. 이는 gonggu_scraper 개발자가 이미 겪고 해결한 문제로, 검증된
해법이 "uc(설치된 진짜 크롬 + 자동화 흔적 제거된 드라이버) + 쿠키가 누적되는 전용 프로필 +
실제 창 + 홈/쇼핑 워밍업"이다. 그 원리를 Windows 전용 코드(winreg/taskkill/COMWAY 경로)를
걷어내고 macOS 기준으로 이식했다.

공용화(2026-08-07): 원래 enrich_detail 전용(naver_uc.py)이었으나, resolve_links의 재검증
크롤링도 같은 네이버 차단을 만나므로 이 엔진을 gonggu.uc_engine으로 끌어올려 둘이 공유한다
(enrich_detail.naver_uc는 이 모듈을 재수출하는 shim). 드라이버 싱글턴/락도 이 모듈 것 하나라,
enrich_detail과 resolve_links가 같은 크롬 창 하나를 직렬로 나눠 쓴다 — 같은 프로필(신뢰 쿠키)을
공유하고 동시에 창이 여러 개 뜨지 않는다.

uc/selenium은 동기(sync)라 드라이버 1개를 락으로 직렬화한다 — 워커가 몇 개든 한 번에 하나씩
처리된다(어차피 같은 도메인에 몰리는 걸 막아야 하니 페이싱 효과도 겸함).

로그인월/챌린지가 뜨면 실제 창에서 사람이 직접 통과하도록 안내하고 기다린다 — 한 번
통과하면 프로필에 쿠키가 저장되어 이후 실행은 자동으로 지나간다(개발자 실증 동작).

환경변수:
    UC_PROFILE               프로필 경로(기본 data/auth/uc_profile — .gitignore의 data/auth/*)
    UC_WAIT                  로드 후 안정화 대기 초(기본 2.5)
    UC_LOGIN_WAIT            로그인월 사람 통과 대기 상한 초(기본 180, 0이면 대기 안 함)
    UC_HEADLESS=1            headless(비권장 — 네이버는 headless 탐지, 기본 0=실제 창)
"""
import os
import re
import subprocess
import threading
import time

from gonggu.common import ROOT

DEFAULT_PROFILE = ROOT / 'data/auth/uc_profile'

# 챌린지/차단 화면 마커 — 네이버 로그인월·영수증 캡차·429 과부하 + 오픈마켓(G마켓/쿠팡 등)
# 봇확인 화면. 실측(2026-08-06): 네이버 "보안 확인을 완료"(띄어쓰기 변형), G마켓 "간단한 봇
# 확인 절차 / 봇(Bot)이란". 공백을 지운 텍스트로 비교(띄어쓰기 변형 대응). 정상 상품
# 페이지에는 안 나오는 문구만 골라 오탐을 막는다.
CHALLENGE_MARKERS = ('보안확인', '실제사용자임을확인', '비정상적인접근', '서비스접속이불가',
                     '자동입력방지', '간단한확인안내', '봇확인', '봇(Bot)이란', '사람이직접조작',
                     'Checkingyourbrowser', 'Justamoment')


def looks_challenged(text_head):
    """HTML/본문 앞부분이 네이버 챌린지 화면인지 — 공백 제거 후 마커 비교(띄어쓰기 변형 대응)."""
    squashed = re.sub(r'\s+', '', text_head or '')
    return any(m in squashed for m in CHALLENGE_MARKERS)

_driver = None
_lock = threading.Lock()


def _detect_chrome_major():
    """설치된 Chrome 메이저 버전(드라이버-브라우저 버전 불일치 예방). macOS는 실행파일에
    --version을 물어보는 게 가장 확실하다. 실패하면 None(uc 자동감지에 맡김)."""
    candidates = (
        '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
        'google-chrome', 'chromium',
    )
    for c in candidates:
        try:
            out = subprocess.check_output([c, '--version'], text=True,
                                          stderr=subprocess.DEVNULL, timeout=10)
            m = re.search(r'(\d+)\.', out)
            if m:
                return int(m.group(1))
        except Exception:
            continue
    return None


def build_driver():
    """uc 드라이버 생성 — 전용 프로필로 실제 크롬을 띄운다. 워밍업 스크립트와 공유."""
    import undetected_chromedriver as uc

    opts = uc.ChromeOptions()
    opts.add_argument('--disable-gpu')
    opts.add_argument('--no-first-run')
    profile = os.environ.get('UC_PROFILE') or str(DEFAULT_PROFILE)
    os.makedirs(profile, exist_ok=True)
    # 스테일 프로필 락 청소 — 이전 uc 프로세스가 비정상 종료(크래시/-9)하면 SingletonLock이 남아
    # 다음 실행이 "cannot connect to chrome / 흰 창"으로 죽는다(2026-08-11 실측). 데일리는 uc
    # 단계를 순차로만 돌리므로(동시 uc 없음) 여기서 지워도 안전하다.
    for _lockname in ('SingletonLock', 'SingletonCookie', 'SingletonSocket'):
        try:
            os.unlink(os.path.join(profile, _lockname))
        except OSError:
            pass
    opts.add_argument(f'--user-data-dir={profile}')
    if os.environ.get('UC_HEADLESS', '0') == '1':
        opts.add_argument('--headless=new')

    kwargs = {}
    major = _detect_chrome_major()
    if major:
        kwargs['version_main'] = major
    driver = uc.Chrome(options=opts, **kwargs)
    driver.set_page_load_timeout(40)
    return driver


def _settle():
    time.sleep(float(os.environ.get('UC_WAIT', '2.5')))


# 사이트별 워밍업 — 홈/검색을 먼저 들러 쿠키·세션을 사람처럼 확보(개발자 uc_driver 원리).
# 대상 호스트별로 1회만. 네이버 외에 오픈마켓(G마켓/옥션)도 uc로 보낼 때 쓴다.
_WARMUP = {
    'naver.': ['https://www.naver.com', 'https://search.shopping.naver.com/'],
    'gmarket.co.kr': ['https://www.gmarket.co.kr/'],
    'auction.co.kr': ['https://www.auction.co.kr/'],
    'coupang.com': ['https://www.coupang.com/'],
    'ohou.se': ['https://ohou.se/', 'https://store.ohou.se/'],
    '11st.co.kr': ['https://www.11st.co.kr/'],
}
_warmed_hosts = set()


def _warm(driver, url=''):
    """대상 URL의 사이트에 맞는 워밍업을 1회 수행. 네이버는 하위호환으로 항상 먼저 데운다."""
    from urllib.parse import urlsplit
    host = urlsplit(url).netloc if url else ''
    for key, steps in _WARMUP.items():
        if (not url and key == 'naver.') or (key in host or (key == 'naver.' and 'naver.' in host)):
            if key in _warmed_hosts:
                continue
            for s in steps:
                try:
                    driver.get(s)
                    _settle()
                except Exception:
                    pass
            _warmed_hosts.add(key)
            return


def _wait_human_pass(driver):
    """로그인월/보안확인이 뜨면 사람이 창에서 직접 통과할 때까지 대기(폴링). 통과하면
    프로필에 쿠키가 남아 다음부터는 자동. UC_LOGIN_WAIT=0이면 기다리지 않는다."""
    limit = float(os.environ.get('UC_LOGIN_WAIT', '180'))
    if limit <= 0:
        return
    def _challenged():
        try:
            url = driver.current_url or ''
        except Exception:
            return False
        if 'nid.naver.com' in url:
            return True
        # ⚠ page_source 앞부분엔 head/script만 있어 캡차 텍스트가 안 잡힌다(실측 2026-08-06 —
        # [:8000]로는 '보안 확인을 완료' 문구를 못 봐서 대기를 건너뛰었음). 렌더된 body의
        # 보이는 텍스트를 직접 본다.
        try:
            body = driver.find_element('tag name', 'body').text
        except Exception:
            body = driver.page_source or ''
        return looks_challenged(body)
    if not _challenged():
        return
    print('\n[!] 네이버 로그인월/보안확인 감지 — 뜬 크롬 창에서 직접 통과해 주세요'
          f'(로그인 또는 확인 버튼, 최대 {int(limit)}초). 통과하면 자동으로 계속됩니다.\n',
          flush=True)
    waited = 0.0
    while waited < limit:
        time.sleep(3.0)
        waited += 3.0
        if not _challenged():
            print('[+] 통과 확인 — 수집을 계속합니다.\n', flush=True)
            time.sleep(1.5)
            return
    print('[!] 대기 시간 내 통과 못 함 — 이 항목은 실패로 남기고 계속합니다.\n', flush=True)


def _scroll_all(driver):
    """끝까지 단계 스크롤 — 상세설명 지연로딩 이미지 로드(원본 uc_driver 원리 그대로)."""
    try:
        last = 0
        for _ in range(8):
            driver.execute_script('window.scrollBy(0, document.body.scrollHeight/8);')
            time.sleep(0.4)
            h = driver.execute_script('return document.body.scrollHeight')
            if h == last:
                break
            last = h
        driver.execute_script('window.scrollTo(0, 0);')
        time.sleep(0.5)
    except Exception:
        pass


def fetch_sync(url):
    """(final_url, html) 반환 — 드라이버 1개를 락으로 직렬화. 실패 시 예외."""
    global _driver
    with _lock:
        if _driver is None:
            _driver = build_driver()
        _warm(_driver, url)
        _driver.get(url)
        _settle()
        _wait_human_pass(_driver)
        _scroll_all(_driver)
        return _driver.current_url, _driver.page_source


def close_sync():
    global _driver, _warmed_hosts
    with _lock:
        d, _driver, _warmed_hosts = _driver, None, set()
        if d is not None:
            try:
                d.quit()
            except Exception:
                pass


import atexit as _atexit  # noqa: E402 — 인터프리터 종료 시 크롬 잔존 방지
_atexit.register(close_sync)
