"""URL의 도메인만 보고 어느 링크인바이오 서비스인지 판별한다.

registry.py(플랫폼→파서 dispatch)와 platforms/linktool.py(linkon/linkseller 공용 파서가
실제 도메인 구분에 필요) 양쪽에서 쓰여서, 순환 임포트를 피하려고 별도 모듈로 둔다.
"""
from __future__ import annotations

from urllib.parse import urlparse

# 도메인 -> 플랫폼 이름. inpk.link/tr.ee는 각각 inpock/linktree의 축약 도메인(동일 구조).
_HOST_TO_PLATFORM = {
    "litt.ly": "littly",
    "link.inpock.co.kr": "inpock",
    "inpk.link": "inpock",
    "linktr.ee": "linktree",
    "tr.ee": "linktree",
    "hity.io": "hity",
    "instabio.cc": "instabio",
    "bio.site": "biosite",
    "linkon.id": "linkon",
    "linkseller.net": "linkseller",
}


def detect_platform(url: str) -> str:
    host = urlparse(url).hostname or ""
    platform = _HOST_TO_PLATFORM.get(host)
    if not platform:
        raise ValueError(f"unsupported host: {host}")
    return platform
