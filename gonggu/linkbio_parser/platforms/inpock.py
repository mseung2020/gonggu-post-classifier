"""인포크(link.inpock.co.kr / inpk.link) — 링크/스마트스토어/컬렉션 블록을 정리한다.

다른 플랫폼과 달리 인포크는 내부 단축경로(/api/r/...)를 자체적으로 갖고 있어, 그 경로만
추가로 최종 주소까지 추적한다(그 외 값은 그대로 둠) — resolve_final_url 하나로는 부족해서
이 파일만의 resolve() 헬퍼를 따로 둔다.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import requests

from ..common import HEADERS, get_username
from ..extract import extract_raw


def parse(url: str, resolve_links: bool = True) -> dict:
    # 도메인이 여러 개(link.inpock.co.kr, inpk.link)라서, 원본 URL에서 scheme+host를
    # 그대로 뽑아 base 주소로 삼는다(단축링크 추적 시 이 base를 붙임).
    base = f"{urlparse(url).scheme}://{urlparse(url).hostname}"
    username = get_username(url)

    res = requests.get(f"{base}/{username}", headers=HEADERS, timeout=10)
    res.raise_for_status()
    page_props = extract_raw("inpock", res.text)
    design = page_props.get("design") or {}
    blocks = page_props.get("blocks") or []

    link_blocks = [b for b in blocks if b.get("block_type") == "link"]
    text_blocks = [b for b in blocks if b.get("block_type") == "text"]
    store_blocks = [b for b in blocks if b.get("block_type") == "smart_store"]
    collection_blocks = [b for b in blocks if b.get("block_type") == "collection"]
    # divider 등 나머지 block_type은 표시용일 뿐 데이터가 없어 무시

    def resolve(path):
        """inpock 내부 단축경로(/api/r/...)만 최종 주소로 추적. 그 외 값은 그대로 둔다."""
        if not path or not path.startswith("/api/r/"):
            return path
        try:
            r = requests.get(f"{base}{path}", headers=HEADERS, allow_redirects=True, timeout=10)
            return r.url
        except requests.RequestException:
            return None

    # link/smart_store/collection 블록의 url과 그 안의 상품 url까지 한 리스트로 모아 한 번에
    # 병렬 resolve한 뒤, 다시 원래 자리로 나눠 담는다(요청을 잘게 나누는 것보다 훨씬 빠름).
    resolve_targets = [b.get("url") for b in link_blocks]
    resolve_targets += [b.get("url") for b in store_blocks]
    for b in store_blocks:
        resolve_targets += [p.get("url") for p in b.get("products", [])]
    for b in collection_blocks:
        resolve_targets += [p.get("url") for p in b.get("links", [])]

    if resolve_links and resolve_targets:
        with ThreadPoolExecutor(max_workers=8) as pool:
            resolved = list(pool.map(resolve, resolve_targets))
    else:
        resolved = [None] * len(resolve_targets)

    it = iter(resolved)
    link_resolved = [next(it) for _ in link_blocks]
    store_resolved = [next(it) for _ in store_blocks]
    store_product_resolved = {b["id"]: [next(it) for _ in b.get("products", [])] for b in store_blocks}
    collection_item_resolved = {b["id"]: [next(it) for _ in b.get("links", [])] for b in collection_blocks}

    smart_stores = [
        {
            "title": b.get("title"),
            "url": b.get("url"),
            "resolved_url": real_url,
            "is_open": b.get("is_open"),
            "products": [
                {
                    "name": p.get("name"),
                    "sale_price": p.get("sale_price"),
                    "discount_price": p.get("discount_price"),
                    "discount_rate": p.get("discount_rate"),
                    "image": p.get("represent_image_url"),
                    "url": p.get("url"),
                    "resolved_url": product_url,
                }
                for p, product_url in zip(b.get("products", []), store_product_resolved[b["id"]])
            ],
        }
        for b, real_url in zip(store_blocks, store_resolved)
    ]

    collections = [
        {
            "title": b.get("title"),
            "is_open": b.get("is_open"),
            "products": [
                {
                    "name": p.get("title"),
                    "price": p.get("price"),
                    "original_price": p.get("original_price"),
                    "image": p.get("image"),
                    "url": p.get("url"),
                    "resolved_url": item_url,
                }
                for p, item_url in zip(b.get("links", []), collection_item_resolved[b["id"]])
            ],
        }
        for b in collection_blocks
    ]

    return {
        "platform": "inpock",
        "source_url": url,
        "username": page_props.get("username"),
        "title": design.get("title"),
        "bio": design.get("bio"),
        "notice": (design.get("notice") or {}).get("contents"),
        "background_color": design.get("background_color"),
        "sns": design.get("sns"),
        "links": [
            {
                "title": b.get("title"),
                "url": b.get("url"),
                "resolved_url": real_url,
                "image": b.get("image"),
                "stickers": [s.get("title") for s in b.get("stickers", [])],
                "is_open": b.get("is_open"),
            }
            for b, real_url in zip(link_blocks, link_resolved)
        ],
        "texts": [b.get("title") for b in text_blocks if b.get("is_open")],
        "smart_stores": smart_stores,
        "collections": collections,
    }
