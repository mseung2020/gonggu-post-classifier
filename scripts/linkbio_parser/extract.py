"""HTML 안에 서버사이드로 박혀 있는 원본 데이터(JSON)를 플랫폼별로 꺼내는 저수준 추출기.

플랫폼마다 데이터를 숨겨 둔 위치가 다르다:
  - litt.ly   : <script id="data"> 안에 base64로 인코딩된 JSON
  - inpock 등 : <script id="__NEXT_DATA__"> 안에 JSON (Next.js 프레임워크 표준)
  - instabio  : JS 코드 안 window.__data = {...}
  - bio.site  : JS 코드 안 window.initial_state = {...}
  - linkon 등 : 숨겨진 <input> 태그 value 속성에 escape된 JSON
"""
from __future__ import annotations

import base64
import html as html_lib
import json
import re


def extract_balanced_json(html: str, marker: str) -> str | None:
    """`window.__data = {...}` 같은 JS 할당문 뒤에 이어지는 balanced JSON을 문자열로 추출한다.

    JSON 뒤에 다른 코드가 붙어 있어 정규식만으로는 "여기서 JSON이 끝난다"를 잡기 어려우므로,
    여는/닫는 괄호 개수를 직접 세어 가며 짝이 맞는 지점을 찾는다(문자열 안의 괄호는 제외).
    """
    idx = html.find(marker)
    if idx == -1:
        return None
    i = html.index("=", idx) + 1
    while i < len(html) and html[i].isspace():
        i += 1
    if i >= len(html) or html[i] not in "{[":
        return None
    open_ch = html[i]
    close_ch = "}" if open_ch == "{" else "]"
    depth = 0
    in_str = False
    esc = False
    start = i
    while i < len(html):
        c = html[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == open_ch:
                depth += 1
            elif c == close_ch:
                depth -= 1
                if depth == 0:
                    return html[start : i + 1]
        i += 1
    return None


def extract_raw(platform: str, html: str) -> dict:
    """플랫폼별로 페이지에 embed된 원본 데이터(JSON)를 추출한 "가공하지 않은" dict."""
    if platform == "littly":
        m = re.search(r'<script id="data" type="text/plain">(.*?)</script>', html, re.S)
        if not m:
            raise ValueError("litt.ly: #data script tag not found (page structure may have changed)")
        return json.loads(base64.b64decode(m.group(1)))

    if platform in ("inpock", "linktree", "hity"):
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
        if not m:
            raise ValueError(f"{platform}: __NEXT_DATA__ script tag not found (page structure may have changed)")
        return json.loads(m.group(1))["props"]["pageProps"]

    if platform == "instabio":
        raw = extract_balanced_json(html, "window.__data")
        if not raw:
            raise ValueError("instabio: window.__data not found")
        return json.loads(raw)

    if platform == "biosite":
        raw = extract_balanced_json(html, "window.initial_state")
        if not raw:
            raise ValueError("biosite: window.initial_state not found")
        return json.loads(raw)

    if platform in ("linkon", "linkseller"):
        # 같은 화이트라벨 솔루션 — 링크 목록이 hidden input(jsonLinkList)에 escape된 JSON으로 embed됨.
        m = re.search(r'id="jsonLinkList"[^>]*value="([^"]*)"', html)
        if not m:
            raise ValueError(f"{platform}: jsonLinkList input not found")
        link_list = json.loads(html_lib.unescape(m.group(1)))
        og_title = re.search(r'property="og:title" content="([^"]*)"', html)
        og_desc = re.search(r'property="og:description" content="([^"]*)"', html)
        return {
            "linkList": link_list,
            "title": html_lib.unescape(og_title.group(1)) if og_title else None,
            "description": html_lib.unescape(og_desc.group(1)) if og_desc else None,
        }

    raise ValueError(f"unsupported platform: {platform}")
