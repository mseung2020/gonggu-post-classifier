"""이미지 URL 수집/정규화 — LLM은 이 파일 근처에도 오지 않는다(코드 전용).

썸네일(대표): 네이버 preload의 representative/optionalImageUrls, JSON-LD image[], og:image.
상세설명: 네이버 SmartEditor(.se-main-container), Cafe24 #prdDetail, 그 외 흔한 상세 컨테이너.
gonggu_scraper 원리 카피: 레이지로딩 실제 URL 속성 우선순위(ec-data-src > data-src >
data-original > src), 1px 스페이서/로딩 플레이스홀더 필터, 순서 유지 중복 제거.
"""
import re
from urllib.parse import urljoin

from .config import MAX_DETAIL_IMAGES, MAX_IMAGE_URL_LEN, MAX_THUMBNAIL_IMAGES

_IMG_ATTRS = ('ec-data-src', 'data-src', 'data-original', 'data-lazy-src', 'src')
_SKIP_IMG = re.compile(r'(1x1|blank|spacer|loading|pixel\.gif|data:image)', re.I)
# 상세설명 영역으로 흔히 쓰이는 컨테이너 — 위에서부터 먼저 매칭된 것 하나만 쓴다(여러 개를
# 합치면 추천상품/리뷰 이미지가 섞인다). 대상몰에서 새 패턴이 보이면 여기에만 추가.
_DETAIL_SELECTORS = ('.se-main-container', '.se-viewer', '#prdDetail', '#productDetail',
                     '#prodDetail', '.goods_detail', '.product-detail', '.detail_cont',
                     '#detail', '.prd-detail', '.item_detail')


def _clean(urls, base_url, cap):
    """절대경로화 + 프로토콜 보정 + 스페이서/비HTTP/과길이 제거 + 순서 유지 dedupe + 상한."""
    out = []
    for u in urls:
        if not u:
            continue
        u = u.strip()
        if _SKIP_IMG.search(u):
            continue
        if u.startswith('//'):
            u = 'https:' + u
        elif u.startswith('/'):
            u = urljoin(base_url, u)
        if not u.startswith(('http://', 'https://')):
            continue
        if len(u) > MAX_IMAGE_URL_LEN:  # 컬럼 폭 초과 — 잘라 넣으면 깨진 링크라 그냥 버림
            continue
        out.append(u)
    return list(dict.fromkeys(out))[:cap]


def _imgs_in(node, base_url):
    out = []
    for im in node.find_all('img'):
        src = next((im.get(a) for a in _IMG_ATTRS if im.get(a)), None)
        if src:
            out.append(urljoin(base_url, src.strip()))
    return out


def collect_thumbnails(candidates, base_url):
    """코드 추출기(preload/JSON-LD/og)가 모아온 대표 이미지 후보를 정리한다."""
    return _clean(candidates, base_url, MAX_THUMBNAIL_IMAGES)


# 컨테이너 셀렉터가 못 잡는 SPA몰(난독화 클래스) 폴백 — 이미지 URL 자체에 "상세설명"임이
# 드러나는 패턴(실측: vyneherb의 S3 경로 product_detailed_descriptions, 2026-08-06).
# 'detail'만으로 걸면 아이콘/배너 오탐이 있어 명시적인 상세설명 경로 패턴만 인정한다.
_DETAIL_URL_PAT = re.compile(r'(detail(?:ed)?[_-]?(?:desc|image|img|content)|/detail/)', re.I)


def collect_detail_images(soup, base_url, pre_collected=None):
    """상세설명 이미지 — 추출기가 이미 찾았으면(네이버 SmartEditor) 그걸 쓰고, 아니면
    흔한 상세 컨테이너에서 첫 매칭 하나를, 그것도 없으면 URL 패턴 폴백으로 긁는다."""
    if pre_collected:
        return _clean(pre_collected, base_url, MAX_DETAIL_IMAGES)
    for sel in _DETAIL_SELECTORS:
        box = soup.select_one(sel)
        if box is not None:
            imgs = _imgs_in(box, base_url)
            if imgs:
                return _clean(imgs, base_url, MAX_DETAIL_IMAGES)
    by_url = [u for u in _imgs_in(soup, base_url) if _DETAIL_URL_PAT.search(u)]
    return _clean(by_url, base_url, MAX_DETAIL_IMAGES)


def build_image_rows(thumbnails, details):
    """image 테이블에 넣을 (image_url, image_type, sort_order) 목록. 썸네일과 상세에 같은
    URL이 있으면 썸네일 쪽만 남긴다(중복 저장 방지)."""
    rows = [(u, 'thumbnail', i) for i, u in enumerate(thumbnails)]
    seen = set(thumbnails)
    order = 0
    for u in details:
        if u in seen:
            continue
        rows.append((u, 'detail', order))
        order += 1
    return rows
