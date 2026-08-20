"""URL의 도메인만 보고 어느 링크인바이오 서비스인지 판별한다.

registry.py(플랫폼→파서 dispatch)와 platforms/linktool.py(linkon/linkseller 공용 파서가
실제 도메인 구분에 필요) 양쪽에서 쓰여서, 순환 임포트를 피하려고 별도 모듈로 둔다.
"""
from __future__ import annotations

from urllib.parse import urlparse

# 도메인 -> 플랫폼 이름. inpk.link/tr.ee는 각각 inpock/linktree의 축약 도메인(동일 구조).
#
# ⚠ 매칭은 "완전 일치 또는 서브도메인"이다(detect_platform 참고) — 여기 키는 **등록 도메인**만
# 적을 것. 예전엔 호스트 이름 완전 일치라서 `계정명.서비스.com` 형태를 쓰는 서비스를 구조적으로
# 하나도 못 잡았다(2026-08-19 실측: 체크포인트 이력에서 LLM#3가 "링크모음"이라 판정했는데 이
# 목록엔 없는 호스트가 129종·752회였고, 그중 linkstory.co.kr은 서브도메인 15개
# (jiy1067·dakkongbebe·ggojunine…), tuk.link는 6개였다). 완전 일치로는 서비스를 목록에 추가해도
# 계정별 주소가 영영 안 잡히므로 매칭 규칙 자체를 바꿨다 — antibot.is_excluded_marketplace와 같은
# 규칙(h == d 또는 h가 '.'+d로 끝남)이다.
_HOST_TO_PLATFORM = {
    "litt.ly": "littly",
    "inpock.co.kr": "inpock",       # link.inpock.co.kr 등
    # inpock의 .com 도메인(2026-08-19 추가) — .co.kr/inpk.link만 있어서 17건이 미등록으로 샜다.
    "inpock.com": "inpock",         # link.inpock.com 등
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


def match_host(host: str) -> str | None:
    """호스트가 지원 서비스에 속하면 플랫폼 이름, 아니면 None.

    "완전 일치 또는 서브도메인"으로 본다 — `jiy1067.linkstory.co.kr`처럼 계정마다 서브도메인이
    다른 서비스를 잡으려면 완전 일치로는 불가능하다(위 _HOST_TO_PLATFORM 주석의 실측 근거).
    가장 긴(=가장 구체적인) 키가 이긴다: `inpk.link`와 `link`가 둘 다 있다면 앞의 것을 고르도록.
    """
    host = (host or "").lower().rstrip(".")
    best = None
    for domain, platform in _HOST_TO_PLATFORM.items():
        if host == domain or host.endswith("." + domain):
            if best is None or len(domain) > len(best[0]):
                best = (domain, platform)
    return best[1] if best else None


def detect_platform(url: str) -> str:
    platform = match_host(urlparse(url).hostname or "")
    if not platform:
        raise ValueError(f"unsupported host: {urlparse(url).hostname or ''}")
    return platform
