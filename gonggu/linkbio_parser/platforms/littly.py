"""litt.ly — 일반 링크(link)와 상품 링크(productLink) 블록을 분리해 정리한다."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import requests

from ..common import HEADERS, get_username, normalize_image_url, resolve_final_url
from ..extract import extract_raw


def parse(url: str, resolve_links: bool = True) -> dict:
    res = requests.get(url, headers=HEADERS, timeout=10)
    res.raise_for_status()
    data = extract_raw("littly", res.text)
    theme = data.get("theme") or {}
    blocks = data.get("blocks") or []

    links = [b for b in blocks if b.get("type") == "link"]
    product_link_blocks = [b for b in blocks if b.get("type") == "productLink"]

    if resolve_links and links:
        with ThreadPoolExecutor(max_workers=8) as pool:
            resolved = list(pool.map(lambda b: resolve_final_url(b.get("url")), links))
    else:
        resolved = [None] * len(links)

    # productLink 블록은 title/price/originalPrice/image/url을 이미 다 담고 있어 리다이렉트 resolve가 필요 없음.
    products = [
        {
            "title": p.get("title"),
            "price": p.get("price"),
            "original_price": p.get("originalPrice"),
            "image": normalize_image_url(p.get("image")),
            "url": p.get("url"),
            "source_type": p.get("type"),  # "coupang" | "naversmartstore" 등
        }
        for b in product_link_blocks
        for p in b.get("links", [])
        if p.get("use", True)  # use가 False인(숨김 처리된) 상품은 제외
    ]

    return {
        "platform": "littly",
        "source_url": url,
        "username": get_username(url),
        "title": None,  # litt.ly 데이터엔 프로필 제목/소개가 마땅치 않아 None
        "bio": None,
        "background_color": theme.get("backgroundColor"),
        "sns": None,
        "links": [
            {
                "title": b.get("title"),
                "url": b.get("url"),
                "resolved_url": real_url,
                "image": b["image"]["url"] if isinstance(b.get("image"), dict) else b.get("image"),
                "folded": b.get("folded"),
                "emphasized": b.get("emphasized"),
            }
            for b, real_url in zip(links, resolved)
        ],
        "products": products,
    }
