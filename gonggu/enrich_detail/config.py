"""enrich_detail 단계 전용 설정값. 환경변수로 조정 가능한 값은 여기서만 읽는다.

크롤링 안전판(MAX_BROWSERS/MAX_PER_DOMAIN/ITEM_DELAY 등)은 resolve_links/config.py의 것을
그대로 공유한다 — 같은 사이트들을 같은 브라우저 풀로 여는 작업이라 상한을 따로 두면
두 단계가 동시에 돌 때 실제 부하가 상한의 2배가 되는 구멍이 생긴다(어차피 daily 안에서는
순차 실행이라 겹치지 않지만, 수동 실행 조합까지 안전하게).
"""
import os

from gonggu.common import DEEPSEEK_MODEL

# 실행 모드(2026-08-12) — 상세 수집을 운영 성격이 정반대인 두 패스로 가른다:
#   fast(기본) — 무인·병렬·안정(requests→Playwright). 자사몰 대량 처리. uc는 절대 안 씀.
#                안티봇에 막힌 상품은 detail_status='blocked'로 남겨 uc 패스에 넘긴다.
#   uc         — 사람이 곁에서·직렬·낮은 동시성(undetected_chromedriver). 'blocked'인 상품만
#                호스트 무관하게 uc로 재시도. 실행 전 warmup_naver_uc로 신뢰 쿠키를 만들어 둔다.
# 두 패스는 순서 barrier 없이 DB 상태('blocked')를 체크포인트로 느슨하게 결합된다.
DETAIL_MODE = os.environ.get('DETAIL_MODE', 'fast').strip().lower()

# 동시 워커 수(= 동시에 처리 중인 상품 수). 실제 브라우저 수는 MAX_BROWSERS가 따로 제한.
# uc 모드는 드라이버 1개를 락으로 직렬화하므로 동시성을 올려도 uc 락에서 줄서기만 하고 이득이
# 없다(오히려 크래시 위험) — 기본 1. fast 모드는 병렬 이득이 있어 기본 4.
DETAIL_CONCURRENCY = int(os.environ.get('DETAIL_CONCURRENCY', '1' if DETAIL_MODE == 'uc' else '4'))

# fast(무인) 모드에서 "어차피 Playwright엔 막히고 uc가 필요"라고 이미 아는 호스트 — 이들은
# Playwright 시도(건당 3~4초 + 네이버 워밍업 재시도)를 낭비하지 않고 곧장 'blocked'로 남겨
# uc 패스에 넘긴다. 목록에 없는 새 호스트는 그대로 시도해 보고, 실제로 막히면 그때 'blocked'가
# 된다(모르는 차단 호스트도 자동 발견 — 화이트리스트를 손으로 관리할 필요 없음).
# 빈 문자열('')로 두면 사전차단 없이 전부 시도한다.
DETAIL_PRESUMED_BLOCK_HOSTS = tuple(
    h.strip() for h in os.environ.get(
        'DETAIL_PRESUMED_BLOCK_HOSTS',
        'naver.,gmarket.co.kr,auction.co.kr,ohou.se,11st.co.kr').split(',') if h.strip())

# LLM#5(상세 요약) 모델. 기본은 프로 — 필드가 많고 형식 제약이 빡빡해서 1차 버전은 프로로
# 품질 기준선을 잡고, 전체 QC에서 플래시 대비 품질/비용을 실측한 뒤에 낮추는 걸 검토한다.
DETAIL_LLM_MODEL = os.environ.get('DETAIL_LLM_MODEL', DEEPSEEK_MODEL)

# LLM#5에 넘기는 상품페이지 본문 텍스트 상한(자). resolve_links의 2000자는 "상품페이지인지
# 판별"용이었지만 여기는 구성/사은품/쿠폰/배송 문구까지 읽어야 해서 더 넉넉히 잡는다.
PAGE_TEXT_LIMIT = int(os.environ.get('DETAIL_PAGE_TEXT', '3500'))
# 캡션도 무한정 넣지 않는다(비정상적으로 긴 캡션 방어).
CAPTION_LIMIT = int(os.environ.get('DETAIL_CAPTION_TEXT', '3000'))

# 이미지 저장 상한 — 상세설명 이미지가 수십 장인 페이지가 실제로 있어서(세로로 긴 상세컷 분할)
# 무제한으로 넣으면 image 테이블이 비대해진다. 넘치면 앞에서부터(화면 순서) 자르고 로그로 알림.
MAX_THUMBNAIL_IMAGES = int(os.environ.get('DETAIL_MAX_THUMBNAILS', '10'))
MAX_DETAIL_IMAGES = int(os.environ.get('DETAIL_MAX_IMAGES', '30'))
# image_url 컬럼이 VARCHAR(500) — 넘는 URL은 잘라 넣으면 깨진 링크가 되므로 그 이미지만 버린다.
MAX_IMAGE_URL_LEN = 500

# 페이지 영구 소멸(gone) 판정 — 보수적으로, "상품이 더 이상 그 주소에 없다"가 명백한 것만.
# '품절'/'판매종료 임박' 같은 문구는 gone이 아니다(페이지는 살아있고 정보도 있음) — 그런 건
# 그대로 done으로 처리하고 내용은 LLM이 요약에 반영한다.
GONE_HTTP_STATUS = (404, 410)
GONE_TEXT_MARKERS = (
    '존재하지 않는 상품', '삭제된 상품', '판매중지된 상품', '판매 중지된 상품',
    '상품을 찾을 수 없', '페이지를 찾을 수 없', '없는 페이지', '상품이 삭제',
)

# detail_error 컬럼 길이(DDL VARCHAR(500)).
MAX_ERROR_LEN = 500
