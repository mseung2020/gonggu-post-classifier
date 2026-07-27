"""instabio.cc — window.__data 안의 컴포넌트(cmpts) 목록에서 버튼형 링크를 뽑는다."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import requests

from ..common import HEADERS, normalize_image_url, resolve_final_url
from ..extract import extract_raw


def parse(url: str, resolve_links: bool = True) -> dict:
    res = requests.get(url, headers=HEADERS, timeout=10)
    res.raise_for_status()
    data = extract_raw("instabio", res.text)
    ui = data.get("ui") or {}  # 프로필 표시 정보(이름/소개 등)
    cmpts = (data.get("content") or {}).get("cmpts") or []  # 컴포넌트(버튼 등) 목록

    # 버튼형 컴포넌트(links[])의 각 링크를 flatten. cmpt 자체 단일 link도 포함.
    raw_links = []
    for c in cmpts:
        for l in c.get("links") or []:
            if l.get("state") == 0:  # state==0 은 비활성/숨김 → 건너뜀
                continue
            raw_links.append({
                "title": l.get("title"),
                "url": l.get("link") or l.get("link1"),  # 키 이름이 두 가지라 or로 처리
                "image": l.get("icon") or c.get("image"),
            })
        if not c.get("links") and c.get("link"):
            raw_links.append({"title": c.get("title"), "url": c.get("link"), "image": c.get("image")})

    raw_links = [l for l in raw_links if l["url"]]

    if resolve_links and raw_links:
        with ThreadPoolExecutor(max_workers=8) as pool:
            resolved = list(pool.map(lambda l: resolve_final_url(l["url"]), raw_links))
    else:
        resolved = [None] * len(raw_links)

    return {
        "platform": "instabio",
        "source_url": url,
        "username": ui.get("username"),
        "title": ui.get("title"),
        "bio": ui.get("desc"),
        "background_color": None,
        "sns": None,
        "links": [
            {
                "title": l["title"],
                "url": l["url"],
                "resolved_url": real_url,
                "image": normalize_image_url(l["image"]),
            }
            for l, real_url in zip(raw_links, resolved)
        ],
        "products": [],
    }
