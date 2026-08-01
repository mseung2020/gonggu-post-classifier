"""링크 해석 단계 전체가 공유하는 설정값/상수. 환경변수로 조정 가능한 값은 여기서만 읽는다."""
import os

from common import DEEPSEEK_MODEL, ROOT

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

# 버튼 텍스트에 이런 말이 있으면 애초에 상품 구매 링크가 아니니 LLM#2한테 보여주지도 않고
# 후보에서 뺀다 — LLM#2 프롬프트에도 같은 취지의 지침이 있지만, 다른 후보가 다 별로면 그중
# "제일 나은" 걸로 고객센터/문의 링크를 골라버리는 경우가 실제로 있어서(확신도 낮게라도)
# 코드 레벨에서 원천적으로 제외한다.
NON_PRODUCT_TEXT = ('고객센터', '고객센타', '고객상담', 'cs', '문의', '상담', '채널톡', '카카오톡',
                     '카카오채널', '공지사항', '이용안내', '배송안내', '교환/환불', '환불정책',
                     '이용약관', '개인정보', '블로그', '유튜브', '인스타그램', '페이스북', '후기',
                     '이벤트', '공식홈페이지')
MAX_CANDIDATES = 80  # cafe.naver.com류 커뮤니티 페이지는 게시판 네비게이션까지 다 잡혀서 넘칠 수 있음
ITEM_DELAY = float(os.environ.get('ITEM_DELAY', '3'))  # 상품 사이 대기(초, 워커별) — 안티봇/레이트리밋 완화
# 워커 수만큼 같은 사이트(네이버/인포크 등)에 동시에 몰리는 실제 요청 빈도가 늘어나므로,
# ITEM_DELAY만으로 완화하던 걸 워커 수까지 감안해서 신중하게 올릴 것 — 진단 라운드로 차단율
# 확인 후 조정.
RESOLVE_CONCURRENCY = int(os.environ.get('RESOLVE_CONCURRENCY', '1'))
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
MAX_BROWSERS = int(os.environ.get('MAX_BROWSERS', str(min(40, (os.cpu_count() or 4) * 4))))
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
