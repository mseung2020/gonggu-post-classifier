"""링크인바이오 파서 전체가 공유하는 상수/저수준 헬퍼(HTTP 헤더, 계정명 추출, 단축링크 추적)."""
from __future__ import annotations

import os
from urllib.parse import urlparse

import requests

# 결과 JSON 파일을 저장할 폴더 — 항상 이 패키지 옆(scripts/linkbio_data/)에 생기게 절대경로로 계산.
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "linkbio_data")

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
HEADERS = {"User-Agent": UA}


def normalize_image_url(image: str | None) -> str | None:
    """litt.ly productLink 이미지는 //domain/... 형태(스킴 없음)로 오는 경우가 있어 https:// 보정."""
    if not image:
        return None
    return f"https:{image}" if image.startswith("//") else image


def get_username(url: str) -> str:
    """URL 경로의 첫 조각을 계정명으로 취급한다(예: litt.ly/hello/world -> hello)."""
    path = urlparse(url).path.strip("/")
    if not path:
        raise ValueError(f"cannot extract username from url: {url}")
    return path.split("/")[0]


def resolve_final_url(url: str) -> str | None:
    """단축/중계 URL을 실제로 접속해 최종 목적지까지 follow. 접속 실패하면 None(예외로 전체를 안 멈춤)."""
    if not url:
        return None
    try:
        res = requests.get(url, headers=HEADERS, allow_redirects=True, timeout=10)
        return res.url
    except requests.RequestException:
        return None
