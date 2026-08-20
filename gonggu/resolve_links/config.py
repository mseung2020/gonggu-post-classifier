"""링크 해석 단계 전체가 공유하는 설정값/상수. 환경변수로 조정 가능한 값은 여기서만 읽는다."""
import os

from gonggu.common import DEEPSEEK_MODEL, ROOT

# append-only(레코드 1개=1줄) — 계속 커지는 체크포인트라 전체를 다시 쓰지 않고 한 줄씩
# 덧붙인다(common.append_jsonl/load_jsonl 참고, 2026-07-27 성능 문제로 .json에서 전환).
RESOLUTION_FILE = ROOT / 'data/output/link_resolution.jsonl'
AUTH_STATE_FILE = ROOT / 'data/auth/session_state.json'

UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36')

BAD_DOMAINS = ('nid.naver.com', 'accounts.kakao.com', 'account.kakao.com', 'mkt.shopping.naver',
               'pf.kakao.com', 'open.kakao.com', 'forms.gle', 'docs.google', 'canva.site', 'band.us',
               'instagram.com', 'youtube.com', 'youtu.be')
# 네이버 블로그는 콘텐츠 페이지일 뿐 실제 구매를 완결할 수 있는 몰이 아니다 — LLM#1이
# url_type을 "네이버_기타" 등으로 잘못 분류했거나 LLM#3가 상품명/가격이 그대로 보인다고
# 상품페이지로 오판해도, 이 도메인이면 최종 구매 링크로 확정하지 않는다(실제 라이브 실행
# 중 발견, 2026-07-20 — 블로그 글이 그대로 done 확정됨).
NON_MALL_DOMAINS = ('blog.naver.com', 'm.blog.naver.com')

# 쿠팡/알리익스프레스/테무 — 제휴·오픈마켓 링크라 공구 대상에서 원천 제외한다(2026-08-11 정책).
# resolve 후보 단계에서 걸러 done이 되지 않게 하고, uc 재검증 대상 호스트에서도 뺀다. 이미
# 적재된 마켓플레이스 링크의 사후 정리는 일회성 purge_marketplace_links가 담당(같은 도메인 기준).
EXCLUDED_MARKETPLACE_DOMAINS = ('coupang.com', 'coupa.ng', 'aliexpress.com', 'aliexpress.us',
                                'temu.com')

# fast(무인) resolve/rescan에서 "브라우저로 열려는 순간" 이 호스트면 크롤을 생략하고 uc 패스
# (reverify_uc)로 넘긴다(2026-08-13, 리졸브 1/2단 분리). 네이버 스마트스토어/오픈마켓처럼
# Playwright엔 로그인월·403/429로 막혀 어차피 unresolved가 되고, 그 시도 하나하나가 브라우저
# 점유 + 호스트 쿨다운(20초)을 먹어 데일리 리졸브 전체를 몇 시간씩 느리게 만든 하드테일이다.
# 여기 걸리면 즉시 '재검증 중 차단' 노트로 남겨 reverify_uc가 uc로 실제로 연다(RESOLVE_UC=1이면
# fast-skip이 꺼져 그 패스에선 정상적으로 uc로 열림). reverify_uc의 RESOLVE_UC_HOSTS 기본값과
# 정합. ⚠ "브라우저를 여는 순간"에만 건다 — 인포크 구조화 데이터로 최종 URL이 이미 확정되는
# (브라우저 안 여는) 경로는 대상이 아니라 그대로 done. 부분 문자열 매칭(host에 'naver.' 포함 등).
UC_LOGINWALL_HOSTS = tuple(h for h in os.environ.get(
    'RESOLVE_SKIP_UC_HOSTS', 'naver.,gmarket.co.kr,auction.co.kr,ohou.se,11st.co.kr').split(',') if h)

# 버튼 텍스트에 이런 말이 있으면 애초에 상품 구매 링크가 아니니 LLM#2한테 보여주지도 않고
# 후보에서 뺀다 — LLM#2 프롬프트에도 같은 취지의 지침이 있지만, 다른 후보가 다 별로면 그중
# "제일 나은" 걸로 고객센터/문의 링크를 골라버리는 경우가 실제로 있어서(확신도 낮게라도)
# 코드 레벨에서 원천적으로 제외한다.
NON_PRODUCT_TEXT = ('고객센터', '고객센타', '고객상담', 'cs', '문의', '상담', '채널톡', '카카오톡',
                     '카카오채널', '공지사항', '이용안내', '배송안내', '교환/환불', '환불정책',
                     '이용약관', '개인정보', '블로그', '유튜브', '인스타그램', '페이스북', '후기',
                     '이벤트', '공식홈페이지')
MAX_CANDIDATES = 80  # cafe.naver.com류 커뮤니티 페이지는 게시판 네비게이션까지 다 잡혀서 넘칠 수 있음

# "링크모음인 건 확실한데 구조화 파서는 없는" 호스트(2026-08-19 추가). linkbio_parser가 지원하는
# 도메인(SUPPORTED_HOSTS)은 브라우저 없이 구조화 데이터로 끝나지만, 이쪽은 파서가 없어서 결국
# 브라우저로 열고 DOM에서 <a>를 긁어야 한다. 그래도 도메인만으로 "이건 링크모음"임을 알 수 있으니
# LLM#3에게 "이 페이지 뭐야?"를 물어보는 홉 하나를 통째로 건너뛴다(그리고 requests 패스트패스로
# 열었다가 링크모음인 걸 알고 브라우저로 다시 여는 중복 페치도 없앤다).
#
# 목록 근거 — 체크포인트 이력 실측(2026-08-19, `python3 -m gonggu._diag_unknown_hubs`로 재생산):
# LLM#3가 "링크모음"이라 판정했는데 SUPPORTED_HOSTS에 없던 호스트가 129종·752회였다. 그중
# "DOM 추출이 실제로 성공하는" 곳만 여기 넣는다 — 추출까지 실패하는 곳(page.im 58/58,
# link.snscommerce.com 12/12, link.favoriit.com 11/11)은 넣어봐야 브라우저만 쓰고 빈손이라 제외.
#   linkstory.co.kr  72회 중 실패 1   (계정별 서브도메인 15종)
#   tuk.link         65회 중 실패 5   (계정별 서브도메인 6종)
#   linkbio.co       57회 중 실패 0
#   wiredy.io        67회 중 실패 28  (절반은 성공 — 넣는 쪽이 이득)
# ⚠ 접미사 매칭이다(is_known_hub 참고) — 등록 도메인만 적을 것. 계정별 서브도메인을 일일이
# 넣으려 하지 말 것(그게 linkbio_parser/hosts.py가 완전일치라서 겪던 바로 그 문제다).
KNOWN_HUB_HOSTS = tuple(h for h in os.environ.get(
    'RESOLVE_KNOWN_HUB_HOSTS', 'linkstory.co.kr,tuk.link,linkbio.co,wiredy.io').split(',') if h)
ITEM_DELAY = float(os.environ.get('ITEM_DELAY', '3'))  # 상품 사이 대기(초, 워커별) — 안티봇/레이트리밋 완화
# ITEM_DELAY를 "이번 항목에서 실제로 브라우저를 쓴 경우"에만 적용(4단계 D1, 2026-08-05).
# 근거: 이 대기는 무거운 브라우저 접근의 안티봇 완화가 목적인데, requests 패스트패스/링크바이오
# 캐시로 끝난 항목까지 일괄로 3초씩 쉬면 워커 가용시간의 상당 부분(실측 약 34%)이 대기로 샌다.
# 민감한 네이버 계열(BROWSER_ONLY_HOSTS)은 애초에 패스트패스를 안 타므로 항상 브라우저 경로
# = 항상 대기가 유지되고, 가벼운 GET의 도메인 몰림은 MAX_PER_DOMAIN이 따로 막는다.
# 차단율이 이상해지면 ITEM_DELAY_SMART=0으로 끄면 예전처럼 매 항목 대기로 돌아간다.
ITEM_DELAY_SMART = os.environ.get('ITEM_DELAY_SMART', '1') != '0'
# 워커 수만큼 같은 사이트(네이버/인포크 등)에 동시에 몰리는 실제 요청 빈도가 늘어나므로,
# ITEM_DELAY만으로 완화하던 걸 워커 수까지 감안해서 신중하게 올릴 것 — 진단 라운드로 차단율
# 확인 후 조정.
RESOLVE_CONCURRENCY = int(os.environ.get('RESOLVE_CONCURRENCY', '1'))
# 브라우저 없는 빠른 패스(Tier0, 2026-08-18 속도개선 공사 F단계) 전용 동시성 — 실측(via_stats)으로
# 후보 URL의 약 80%(linkbio_structured+uc_host_skip+http 합)가 브라우저를 아예 안 쓰고 끝나는데,
# 예전엔 이 80%도 RESOLVE_CONCURRENCY(브라우저 필요 20%까지 감안해 낮게 잡은 값)라는 좁은 슬롯
# 수만큼만 병렬화됐다. Tier0은 브라우저/Playwright 드라이버 자체를 안 띄우므로(runner.py의
# use_playwright=False 패스) MAX_BROWSERS와 무관하게 네트워크·호스트쿨다운만이 병목이라 훨씬
# 높게 잡을 수 있다. 브라우저가 필요하다고 판정된 나머지만 RESOLVE_CONCURRENCY/MAX_BROWSERS로
# 넘어가는 Tier1(기존 경로)에서 처리한다.
RESOLVE_FAST_CONCURRENCY = int(os.environ.get('RESOLVE_FAST_CONCURRENCY', '200'))
# RESOLVE_CONCURRENCY(워커 스레드 수)와 별개로, 실제로 떠 있는 Playwright 브라우저 프로세스
# 개수의 하드웨어 안전판 — 이 상한이 없으면 최악의 경우(워커가 전부 동시에 브라우저를 필요로
# 하는 순간) 워커 수만큼 크롬 프로세스가 떠서 메모리를 통째로 먹는다(실측 확인, 2026-07-30 —
# 워커 200개 = 크롬 관련 프로세스 550개+, 스왑 32GB 소진으로 시스템 전체가 먹통이 됨).
#
# ⚠ 이건 "동시 브라우저 수"의 상한이지 "동시 워커 수"의 상한이 아니다. 다만 브라우저가 필요한
# 작업의 처리량은 결국 이 값이 정한다 — 워커를 200개로 올려도 브라우저 작업은 이 수만큼만
# 동시에 돈다. 예전 LazyPage는 브라우저를 다 쓴 뒤에도 워커가 끝날 때까지 허가증을 쥐고 있어서
# 대기자가 값싼 건조차 처리하지 못했고, 지금은 release_if_contended로 놓아준다.
# 허가증 쟁탈이 잦으면 재기동(3.9초)이 반복되므로 워커 수와 이 값의 차이가 너무 벌어지지 않게
# 두는 게 좋다(runner가 시작 시 경고).
def _default_max_browsers():
    """브라우저 동시 개수 기본값 — CPU뿐 아니라 RAM도 본다(2026-08-11). 크롬 하나가 수백 MB~1GB를
    먹어서, 16GB 맥에서 CPU 기준 40개를 띄우면 스왑으로 넘어가 시스템이 먹통이 된다(실측 사고).
    RAM 1.5GB당 브라우저 1개를 여유로 잡고, 하드 상한 16·최소 4로 클램프. MAX_BROWSERS 환경변수로 덮어쓸 수 있다."""
    cpu_cap = (os.cpu_count() or 4) * 4
    try:
        ram_gb = os.sysconf('SC_PHYS_PAGES') * os.sysconf('SC_PAGE_SIZE') / (1024 ** 3)
        ram_cap = int(ram_gb // 1.5)
    except (ValueError, OSError, AttributeError):
        ram_cap = 8
    return max(4, min(cpu_cap, ram_cap, 16))


MAX_BROWSERS = int(os.environ.get('MAX_BROWSERS', str(_default_max_browsers())))
# 브라우저를 띄우기 전에 requests로 먼저 시도할지(httpfetch.py 참고). 판별에 쓰는 정보가
# 충분히 나오면 그대로 쓰고, 모자라거나 차단되면 자동으로 브라우저 경로로 넘어간다 —
# 판정이 이상해지면 HTTP_FAST_PATH=0으로 끄고 예전 동작(항상 브라우저)으로 되돌릴 수 있다.
HTTP_FAST_PATH = os.environ.get('HTTP_FAST_PATH', '1') != '0'
# LLM#2(링크선택)/LLM#3(페이지판별)에 쓰는 모델.
#
# ⚠ 플래시로 바꾸는 건 이미 시도해봤고, 실행 시간이 오히려 늘어서 되돌렸다(실측, 2026-08-01 —
# 60건/워커 15개로 순서를 바꿔가며 4회). LLM 누적 시간은 1311초 -> 886초로 32% 줄었는데
# 정작 실제 소요 시간은 프로 196초/196초 vs 플래시 202초/278초였다. 원인은 평균이 아니라
# 꼬리다 — 호출 하나가 60~117초씩 걸리는 경우가 있고(플래시가 더 심함: 최대 71~117초 vs
# 프로 60초), 그런 한 건이 안 끝나면 나머지가 다 끝나도 전체가 안 끝난다. 즉 이 단계는
# "평균을 깎아서" 빨라지지 않는다. 꼬리를 자르거나(call_llm 타임아웃/재시도) 대기 구간
# (domain_gate·ITEM_DELAY, 워커 가용시간의 약 34%)을 손대야 한다.
# 요금을 아끼는 게 목적이라면 플래시도 선택지다 — 품질은 같은 40건에서 done 15건(플래시) vs
# 13건(프로)으로 차이가 없었다(동일 설정 재실행 시 일치율이 88%인 LLM 자체 변동성 범위 안).
LINK_LLM_MODEL = os.environ.get('LINK_LLM_MODEL', DEEPSEEK_MODEL)
# LLM#2/#3 꼬리 지연 자르기(4단계 D3, 옵트인) — 위 실측대로 이 단계의 전체 시간은 평균이 아니라
# 60~117초짜리 꼬리 호출이 정한다. LINK_LLM_TIMEOUT을 예컨대 45로 낮추고
# LINK_LLM_TIMEOUT_RETRY=1을 주면 타임아웃 시 한 번 다시 시도한다. ⚠ 재시도는 LLM 비결정성
# 때문에 같은 입력에도 답이 달라질 수 있어(어차피 지금도 타임아웃→error→rescan 재시도로
# 완전 결정론은 아님) 기본은 꺼져 있고(120초, 재시도 없음 — 기존과 동일) 명시적으로 켜야 한다.
LINK_LLM_TIMEOUT = float(os.environ.get('LINK_LLM_TIMEOUT', '120'))
LINK_LLM_TIMEOUT_RETRY = int(os.environ.get('LINK_LLM_TIMEOUT_RETRY', '0'))
HTTP_FETCH_TIMEOUT = (5, float(os.environ.get('HTTP_FETCH_TIMEOUT', '12')))  # (connect, read)
# 본문을 전부 JS로 그리는 몰(실측, 2026-08-01 — store.kakao.com은 og 태그는 다 있는데 body
# 텍스트가 0자)을 걸러내는 최소 본문 길이. LLM#3이 본문 속 "정가/공구가"를 판별 근거로 쓰기
# 때문에, 본문이 비면 브라우저로 열었을 때와 판정이 달라진다 — 그런 페이지는 브라우저로 넘긴다.
HTTP_MIN_BODY_TEXT = int(os.environ.get('HTTP_MIN_BODY_TEXT', '200'))
# bare requests에 429/봇차단을 주는 호스트 — 어차피 실패할 요청을 보내 네이버 쪽 레이트리밋
# 카운터만 올릴 이유가 없어서 아예 건너뛰고 바로 브라우저로 간다(실측, 2026-08-01 —
# smartstore/brand.naver.com 둘 다 requests 단독 호출에 429). 브라우저 경로는 저장된 세션
# 쿠키(AUTH_STATE_FILE)와 stealth가 있어서 통과한다. 서브도메인까지 접미사 매칭한다.
BROWSER_ONLY_HOSTS = tuple(h for h in os.environ.get(
    'BROWSER_ONLY_HOSTS', 'naver.com,naver.me').split(',') if h)
# 후보 링크가 스마트스토어/인포크 등 몇 개 도메인에 몰려 있어서 "도메인당 동시 1개"로 막으면
# 워커를 늘려도 대부분 대기만 하게 된다(실측 확인, 2026-07-27 — 동시 30인데 도메인 락 때문에
# 2.8배밖에 안 빨라짐) — 그래서 도메인당 동시 허용치를 살짝 풀어주되, 여전히 상한을 둬서
# 같은 사이트에 몰리는 정도는 제한한다.
MAX_PER_DOMAIN = int(os.environ.get('MAX_PER_DOMAIN', '4'))
BLOCKED_STATUS_CODES = (403, 429, 490)  # 490=네이버 캡차/보안확인
BLOCKED_TEXT_MARKERS = ('security verification', '보안확인을 완료', 'unusual traffic', '비정상적인 접근')
SLOW_REDIRECT_DOMAINS = ('mkt.shopping.naver.com',)
# 검증 홉이 없어서 여기서 확정하면 그대로 DB에 들어가므로, 링크모음/스토어메인 둘 다
# 이 확신도 이상일 때만 최종 채택한다(low는 자동 확정 안 함).
LINK_PICK_OK_CONF = os.environ.get('LINK_PICK_OK_CONF', 'high,medium').split(',')

# ranking.py가 "확정몰"로 취급하는 도메인 — 이 목록에 있으면 크롤링 없이도 실제 구매 가능한
# 채널로 신뢰하고 우선순위를 높인다(링크인바이오 허브 다음으로).
MALL_DOMAINS = ('smartstore.naver.com', 'm.smartstore.naver.com', 'brand.naver.com',
                 'shopping.naver.com', 'coupang.com', 'www.coupang.com', 'gmarket.co.kr',
                 'auction.co.kr', '11st.co.kr', 'interpark.com')

DISCONTINUED_MARKERS = ('discontinued', 'soldout', 'sold-out', 'sold_out')
# 앱/SPA가 잘못된 딥링크를 자기 도메인의 범용 에러 페이지로 돌리면서도 HTTP 200을 주는
# 경우(라이브 실행 중 발견, 2026-07-20 — hi.thehyundai.com/error가 그대로 done 확정됨).
# "error"를 URL 어디서나 부분일치로 찾으면 정상 상품 경로(예: /error-resistant-widget)까지
# 오탐할 수 있어, 경로 전체가 이 값과 정확히 같을 때만 잡는다.
BROKEN_PATH_SEGMENTS = ('error', 'notfound', 'not-found', '404')
