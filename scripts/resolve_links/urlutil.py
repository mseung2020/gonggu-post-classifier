"""URL 문자열만 다루는 최소 단위 유틸 — 다른 resolve_links 서브모듈들이 순환 임포트 없이
공유할 수 있도록 별도 모듈로 둔다(browser.py/antibot.py 양쪽에서 필요)."""
from urllib.parse import urlparse


def host_of(url):
    try:
        return urlparse(url).netloc
    except Exception:
        return ''
