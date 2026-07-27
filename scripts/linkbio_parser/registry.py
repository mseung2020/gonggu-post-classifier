"""플랫폼 이름 -> 파서(parse) 함수를 연결하는 dispatch table과 공개 진입점(parse/fetch_raw/save)."""
from __future__ import annotations

import json
import os

import requests

from .common import HEADERS, OUTPUT_DIR, get_username
from .extract import extract_raw
from .hosts import detect_platform
from .platforms import biosite, hity, inpock, instabio, littly, linktool, linktree

# 플랫폼 이름 -> 그 플랫폼을 처리하는 parse 함수(전략 패턴 dispatch table).
# linkon/linkseller는 화이트라벨이라 parse 함수 하나(linktool.parse)를 공유한다.
PARSERS = {
    "littly": littly.parse,
    "inpock": inpock.parse,
    "linktree": linktree.parse,
    "hity": hity.parse,
    "instabio": instabio.parse,
    "biosite": biosite.parse,
    "linkon": linktool.parse,
    "linkseller": linktool.parse,
}


def parse(url: str, **kwargs) -> dict:
    """url값(사용자명, 리다이렉트 토큰 등)은 계속 바뀌므로 항상 이 함수로 실시간 조회할 것 —
    결과를 캐싱해서 재사용하지 말 것. 대표 진입점 — 안에서 플랫폼을 판별해 알맞은 파서로 위임한다."""
    platform = detect_platform(url)
    return PARSERS[platform](url, **kwargs)


def fetch_raw(url: str) -> dict:
    """가공하지 않은 원본 데이터(JSON) 그대로 반환 — "우리가 정리한 결과가 이상한데?" 싶을 때
    원본과 대조하려고 남겨 두는 용도(run_batch(save_raw=True)일 때만 호출)."""
    platform = detect_platform(url)
    res = requests.get(url, headers=HEADERS, timeout=10)
    res.raise_for_status()
    raw = extract_raw(platform, res.text)
    return {"platform": platform, "source_url": url, "username": get_username(url), "raw": raw}


def save(data: dict, out_dir: str = OUTPUT_DIR, suffix: str = "") -> str:
    """결과 dict를 "플랫폼_계정명.json" 파일로 저장하고, 저장 경로를 돌려준다."""
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"{data['platform']}_{data['username']}{suffix}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return out_path
