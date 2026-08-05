"""linktr.ee / tr.ee — 링크 목록 + (있으면) 상품 판매형 진열대(commerceStorefrontItems)."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import requests

from ..common import HEADERS, get_username, resolve_final_url
from ..extract import extract_raw


def parse(url: str, resolve_links: bool = True) -> dict:
    res = requests.get(url, headers=HEADERS, timeout=10)
    res.raise_for_status()
    page_props = extract_raw("linktree", res.text)

    # HEADER 타입은 섹션 구분용 텍스트일 뿐 url이 없어 제외
    link_blocks = [b for b in (page_props.get("links") or []) if b.get("type") != "HEADER" and b.get("url")]

    if resolve_links and link_blocks:
        with ThreadPoolExecutor(max_workers=8) as pool:
            resolved = list(pool.map(lambda b: resolve_final_url(b.get("url")), link_blocks))
    else:
        resolved = [None] * len(link_blocks)

    # 상품 판매형(commerceStorefrontItems)은 샘플 계정에 데이터가 없어 필드 구조 미검증 — 존재 시 방어적으로만 추출
    storefront = page_props.get("commerceStorefrontItems") or {}
    products = [
        {
            "title": item.get("title") or item.get("name"),
            "price": item.get("price"),
            "image": item.get("image") or item.get("thumbnail"),
            "url": item.get("url"),
        }
        for item in storefront.get("items", [])
    ]

    return {
        "platform": "linktree",
        "source_url": url,
        "username": page_props.get("username") or get_username(url),
        "title": page_props.get("pageTitle"),
        "bio": page_props.get("description"),
        "background_color": None,
        "sns": page_props.get("socialLinks"),
        "links": [
            {
                "title": b.get("title"),
                "url": b.get("url"),
                "resolved_url": real_url,
                "image": b.get("thumbnail"),
            }
            for b, real_url in zip(link_blocks, resolved)
        ],
        "products": products,
    }
