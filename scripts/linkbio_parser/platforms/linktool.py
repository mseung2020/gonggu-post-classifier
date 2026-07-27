"""linkon.id / linkseller.net — 같은 화이트라벨 솔루션을 도메인만 바꿔 파는 것이라 파서 하나가
둘 다 담당한다(detect_platform으로 실제 도메인을 구분해 platform 필드만 다르게 채움)."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import requests

from ..common import HEADERS, get_username, resolve_final_url
from ..extract import extract_raw
from ..hosts import detect_platform


def parse(url: str, resolve_links: bool = True) -> dict:
    platform = detect_platform(url)
    base = f"{urlparse(url).scheme}://{urlparse(url).hostname}"
    username = get_username(url)

    res = requests.get(url, headers=HEADERS, timeout=10)
    res.raise_for_status()
    raw = extract_raw(platform, res.text)

    # boxtype이 link인 항목만 (text/schedule/ad/cslink 등은 링크 아님)
    link_items = [b for b in raw["linkList"] if b.get("boxtype") == "link" and b.get("lpl_url")]

    if resolve_links and link_items:
        with ThreadPoolExecutor(max_workers=8) as pool:
            resolved = list(pool.map(lambda b: resolve_final_url(b.get("lpl_url")), link_items))
    else:
        resolved = [None] * len(link_items)

    def image_url(fname):
        """썸네일 파일명(ll_img)만 있고 전체 주소는 없어서, 렌더링된 img src 규칙대로 조립한다."""
        return f"{base}/ico/{username}/thum2/{fname}" if fname else None

    return {
        "platform": platform,
        "source_url": url,
        "username": username,
        "title": raw.get("title"),
        "bio": raw.get("description"),
        "background_color": None,
        "sns": None,
        "links": [
            {
                "title": b.get("ll_name"),  # 이 서비스는 컬럼명이 ll_name/lpl_url
                "url": b.get("lpl_url"),
                "resolved_url": real_url,
                "image": image_url(b.get("ll_img")),
            }
            for b, real_url in zip(link_items, resolved)
        ],
        "products": [],
    }
