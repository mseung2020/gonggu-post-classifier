"""수동 테스트용 CLI. scripts/ 디렉터리에서:
    python3 -m linkbio_parser                 # 인자 없으면 TEST_URLS 사용
    python3 -m linkbio_parser urls.txt        # 파일의 URL 목록 사용
    python3 -m linkbio_parser https://a https://b
"""
import sys

from .batch import collect_urls, run_batch

# 테스트용 — 여기 URL을 직접 추가/삭제하면서 테스트.
TEST_URLS = [
    "https://link.inpock.co.kr/181213_hy",
]

# True로 바꾸면 가공된 결과와 별개로 원본 JSON도 linkbio_data/raw/ 에 저장.
SAVE_RAW = True

urls = collect_urls(sys.argv[1:]) if len(sys.argv) > 1 else TEST_URLS
run_batch(urls, save_raw=SAVE_RAW)
