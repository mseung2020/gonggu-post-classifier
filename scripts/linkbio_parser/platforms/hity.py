"""hity.io — 페이지는 여러 "섹션(section)"으로 구성되고, 섹션마다 링크/상품 목록을 담는다."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import requests

from ..common import HEADERS, get_username, resolve_final_url
from ..extract import extract_raw


def parse(url: str, resolve_links: bool = True) -> dict:
    res = requests.get(url, headers=HEADERS, timeout=10)
    res.raise_for_status()
    page_props = extract_raw("hity", res.text)
    space = page_props.get("space") or {}  # hity는 페이지를 "space"라고 부른다
    sections = space.get("sections") or []

    # 각 섹션의 링크 정보(spaceSectionLinkInfos)를 flatten. link.target이 실제 목적지.
    raw_links = []
    for sec in sections:
        for info in sec.get("spaceSectionLinkInfos") or []:
            link = info.get("link") or {}
            raw_links.append({
                "title": info.get("title") or info.get("description"),
                "url": link.get("target"),
                "image": info.get("imageUrl"),
                "section_type": sec.get("type"),
            })
    raw_links = [l for l in raw_links if l["url"]]

    if resolve_links and raw_links:
        with ThreadPoolExecutor(max_workers=8) as pool:
            resolved = list(pool.map(lambda l: resolve_final_url(l["url"]), raw_links))
    else:
        resolved = [None] * len(raw_links)

    # 상품 판매(spaceSectionShopInfos)는 샘플 계정에 데이터가 없어 구조 미검증 — 존재 시 방어적으로만 추출
    products = []
    for sec in sections:
        for shop in sec.get("spaceSectionShopInfos") or []:
            link = shop.get("link")
            products.append({
                "title": shop.get("title") or shop.get("name"),
                "price": shop.get("price") or shop.get("salePrice"),
                "image": shop.get("imageUrl") or shop.get("image"),
                "url": link.get("target") if isinstance(link, dict) else link,
            })

    return {
        "platform": "hity",
        "source_url": url,
        "username": get_username(url),
        "title": space.get("title"),
        "bio": space.get("description"),
        "background_color": None,
        "sns": None,
        "links": [
            {
                "title": l["title"],
                "url": l["url"],
                "resolved_url": real_url,
                "image": l["image"],
                "section_type": l["section_type"],
            }
            for l, real_url in zip(raw_links, resolved)
        ],
        "products": products,
    }
