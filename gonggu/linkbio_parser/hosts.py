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


# 지원 도메인 전체를 공개 상수로도 노출한다 — prompts.py의 LLM#1/유튜브 PPL 프롬프트가
# "링크모음" 판별 예시로 도메인 목록을 하드코딩해서 들고 있었는데, 실제 지원 목록(위
# _HOST_TO_PLATFORM)과 따로 놀아 lit.link·taplink처럼 지원하지 않는 도메인이 예시로 남아있고
# hity.io/instabio.cc/bio.site/linkon.id/linkseller.net처럼 실제 지원하는 도메인은 빠져있는
# 드리프트가 있었다(2026-08-18 점검, 문제 11). 프롬프트가 여기서 동적으로 만들어 가져가게
# 해서 이 목록이 유일한 출처가 되게 한다.
SUPPORTED_HOSTS = tuple(_HOST_TO_PLATFORM.keys())


def detect_platform(url: str) -> str:
    host = urlparse(url).hostname or ""
    platform = _HOST_TO_PLATFORM.get(host)
    if not platform:
        raise ValueError(f"unsupported host: {host}")
    return platform
