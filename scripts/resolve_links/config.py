"""링크 해석 단계 전체가 공유하는 설정값/상수. 환경변수로 조정 가능한 값은 여기서만 읽는다."""
import os

from common import ROOT

RESOLUTION_FILE = ROOT / 'data/output/link_resolution.json'
AUTH_STATE_FILE = ROOT / 'data/auth/session_state.json'

DIFY_KEY_PICK = os.environ.get('DIFY_KEY_PICK', '')
DIFY_KEY_JUDGE = os.environ.get('DIFY_KEY_JUDGE', '')

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
BLOCKED_STATUS_CODES = (403, 429, 490)  # 490=네이버 캡차/보안확인
BLOCKED_TEXT_MARKERS = ('security verification', '보안확인을 완료', 'unusual traffic', '비정상적인 접근')
SLOW_REDIRECT_DOMAINS = ('mkt.shopping.naver.com',)
# 검증 홉이 없어서 여기서 확정하면 그대로 DB에 들어가므로, 링크모음/스토어메인 둘 다
# 이 확신도 이상일 때만 최종 채택한다(low는 자동 확정 안 함).
LINK_PICK_OK_CONF = os.environ.get('LINK_PICK_OK_CONF', 'high,medium').split(',')

# url_type(LLM#1이 판단한 대표 구매 URL 종류)과 실제 도메인을 매칭시키는 힌트 — 후보가 여러 개일 때
# 무관한 링크를 먼저 집어서 오판하는 걸 방지 (dify_workflows/01_gonggu_classify.yml의 url_type enum과 대응)
URL_TYPE_DOMAIN_HINTS = {
    '네이버_스마트스토어': ('smartstore.naver.com', 'brand.naver.com', 'shopping.naver.com'),
    '네이버_기타': ('naver.com',),
    '쿠팡_오픈마켓': ('coupang.com', 'gmarket.co.kr', 'auction.co.kr', '11st.co.kr', 'interpark.com'),
    '카카오채널': ('kakao.com',),
}

DISCONTINUED_MARKERS = ('discontinued', 'soldout', 'sold-out', 'sold_out')
# 앱/SPA가 잘못된 딥링크를 자기 도메인의 범용 에러 페이지로 돌리면서도 HTTP 200을 주는
# 경우(라이브 실행 중 발견, 2026-07-20 — hi.thehyundai.com/error가 그대로 done 확정됨).
# "error"를 URL 어디서나 부분일치로 찾으면 정상 상품 경로(예: /error-resistant-widget)까지
# 오탐할 수 있어, 경로 전체가 이 값과 정확히 같을 때만 잡는다.
BROKEN_PATH_SEGMENTS = ('error', 'notfound', 'not-found', '404')
