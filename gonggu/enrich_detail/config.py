"""enrich_detail 단계 전용 설정값. 환경변수로 조정 가능한 값은 여기서만 읽는다.

크롤링 안전판(MAX_BROWSERS/MAX_PER_DOMAIN/ITEM_DELAY 등)은 resolve_links/config.py의 것을
그대로 공유한다 — 같은 사이트들을 같은 브라우저 풀로 여는 작업이라 상한을 따로 두면
두 단계가 동시에 돌 때 실제 부하가 상한의 2배가 되는 구멍이 생긴다(어차피 daily 안에서는
순차 실행이라 겹치지 않지만, 수동 실행 조합까지 안전하게).
"""
import os

from gonggu.common import DEEPSEEK_MODEL

# 동시 워커 수(= 동시에 처리 중인 상품 수). 실제 브라우저 수는 MAX_BROWSERS가 따로 제한.
DETAIL_CONCURRENCY = int(os.environ.get('DETAIL_CONCURRENCY', '4'))

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
