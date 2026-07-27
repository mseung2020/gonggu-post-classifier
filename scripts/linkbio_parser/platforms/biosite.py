"""bio.site — window.initial_state 안의 "section_links" 섹션에서 링크를 모은다."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import requests

from ..common import HEADERS, get_username, resolve_final_url
from ..extract import extract_raw


def parse(url: str, resolve_links: bool = True) -> dict:
    res = requests.get(url, headers=HEADERS, timeout=10)
    res.raise_for_status()
    data = extract_raw("biosite", res.text)
    header = data.get("header") or {}  # 프로필 헤더(이름/소개)
    body = data.get("body") or []  # 본문 섹션들

    raw_links = []
    for sec in body:
        if sec.get("type") == "section_links":
            for l in (sec.get("section") or {}).get("links") or []:
                raw_links.append({
                    "title": l.get("name") or l.get("title"),
                    "url": l.get("url"),
                    "image": l.get("image") or l.get("thumbnail"),
                })
    raw_links = [l for l in raw_links if l["url"]]

    if resolve_links and raw_links:
        with ThreadPoolExecutor(max_workers=8) as pool:
            resolved = list(pool.map(lambda l: resolve_final_url(l["url"]), raw_links))
    else:
        resolved = [None] * len(raw_links)

    # 소셜 핸들 — "section_social" 섹션을 찾으면 그 안의 handles를 쓴다.
    sns = []
    for sec in body:
        if sec.get("type") == "section_social":
            sns = (sec.get("section") or {}).get("handles")
            break

    return {
        "platform": "biosite",
        "source_url": url,
        "username": (data.get("metadata") or {}).get("handle") or get_username(url),
        "title": header.get("name"),
        "bio": header.get("bio"),
        "background_color": None,
        "sns": sns,
        "links": [
            {"title": l["title"], "url": l["url"], "resolved_url": real_url, "image": l["image"]}
            for l, real_url in zip(raw_links, resolved)
        ],
        "products": [],
    }
