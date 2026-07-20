"""
================================================================================
 링크인바이오(link-in-bio) 페이지 파서
================================================================================

[이 스크립트가 하는 일 — 한 문장 요약]
  인스타그램 프로필 등에 걸려 있는 "링크 모음 페이지" 주소(URL)를 받아서,
  그 안에 들어 있는 링크/상품/프로필 정보를 깔끔한 JSON 형태로 뽑아낸다.

[배경 지식: 링크인바이오란?]
  인스타그램은 게시물 본문에 클릭 가능한 링크를 못 넣는다. 그래서 사람들은
  litt.ly, 링크트리(linktr.ee), 인포크(inpock) 같은 서비스에서 "링크 모음
  페이지"를 하나 만들어 두고, 프로필에 그 주소 하나만 걸어 둔다.
  이 스크립트는 그런 링크 모음 페이지들을 자동으로 읽어 오는 도구다.

[핵심 아이디어 — 왜 이렇게 만들었나]
  이런 페이지들은 대부분 "서버가 미리 만든 HTML"을 내려주는데, 그 HTML 안
  어딘가에 페이지를 그리는 데 쓰인 원본 데이터(JSON)가 통째로 박혀 있다.
  (브라우저 개발자도구로 페이지 소스를 보면 <script> 태그 안에 데이터가 보임)
  따라서 우리는 브라우저를 흉내 낼 필요 없이, HTML을 그냥 받아서(requests)
  그 안에 박힌 JSON만 골라 꺼내면 된다. 별도 공식 API 없이 동작한다.

[플랫폼마다 데이터를 숨겨 둔 위치가 다르다]
  - litt.ly      : <script id="data"> 안에 base64로 인코딩된 JSON
  - inpock 등    : <script id="__NEXT_DATA__"> 안에 JSON (Next.js 프레임워크 표준)
  - instabio     : JS 코드 안 window.__data = {...}
  - bio.site     : JS 코드 안 window.initial_state = {...}
  - linkon 등    : 숨겨진 <input> 태그 value 속성에 escape된 JSON
  그래서 "위치 찾아 꺼내기(extract_*)"와 "플랫폼별로 예쁘게 정리(parse_*)"를
  플랫폼별 함수로 나눠 두었다.

[전체 처리 흐름]
  URL 목록
    → parse(url)               # 어느 플랫폼인지 판별 후 알맞은 parser 호출
        → detect_platform()    # 도메인(host)만 보고 플랫폼 이름 결정
        → parse_littly / parse_inpock / ...   # 플랫폼별 파서
            → requests.get()   # HTML 다운로드
            → 정규식/JSON 파싱  # HTML 속 데이터 꺼내기
            → resolve_final_url()  # 단축링크의 진짜 목적지 추적(선택)
    → save()                   # 결과 JSON을 linkbio_data/ 폴더에 파일로 저장

[결과 JSON의 공통 형태]
  모든 parser는 아래 형태의 dict를 돌려주도록 통일했다(플랫폼마다 필드 조금씩 추가):
    {
      "platform": "inpock",        # 어느 서비스인지
      "source_url": "...",         # 우리가 조회한 원본 주소
      "username": "...",           # 그 페이지 주인의 계정명
      "title": "...", "bio": "...",# 프로필 제목/소개글
      "links": [ {title, url, resolved_url, image, ...}, ... ],  # 링크 목록
      "products": [ ... ],         # (일부 플랫폼) 상품 목록
    }
================================================================================
"""
# 원본(개발자가 공유해준 parse_linkbio.py)은 `str | None` 같은 3.10+ 문법을 쓰는데 이
# 프로젝트는 3.9라 그대로는 안 돌아간다 — 이 한 줄이면 애너테이션이 실행 시점에 평가되지
# 않고 문자열로만 남아서(3.9에서도) 그대로 통과한다. 이 줄 외엔 원본 그대로.
from __future__ import annotations

# ── 표준 라이브러리(파이썬 기본 내장) ──────────────────────────────────────
import base64  # base64로 인코딩된 데이터를 원래대로 디코딩 (litt.ly에서 사용)
import html as html_lib  # HTML escape 문자(&amp; 등)를 원래 문자로 복원. 'html'은

#   변수명과 겹치기 쉬워서 html_lib라는 별칭으로 불러온다.
import json  # 문자열 ↔ 파이썬 dict/list 변환 (JSON 파싱/저장)
import os  # 파일 경로 조합, 폴더 생성 등 운영체제 관련 작업
import random  # 요청 사이 대기 시간을 무작위로 주기 위해 사용
import re  # 정규식(regular expression). HTML에서 특정 패턴을 찾을 때 사용
import sys  # 커맨드라인 인자(sys.argv)를 읽기 위해 사용
import time  # time.sleep()으로 잠깐 쉬어 가기 위해 사용
from concurrent.futures import (
    ThreadPoolExecutor,  # 여러 링크를 동시에(병렬로) 처리하기 위한 도구
)
from urllib.parse import urlparse  # URL을 scheme/host/path 등으로 쪼개 주는 도구

# ── 외부 라이브러리(pip install 필요) ──────────────────────────────────────
import requests  # HTTP 요청 라이브러리. 웹페이지 HTML을 받아 오는 데 사용

# 결과 JSON 파일을 저장할 폴더 경로.
#   os.path.abspath(__file__)  : 이 .py 파일의 절대경로
#   os.path.dirname(...)       : 그 파일이 들어 있는 폴더
#   os.path.join(..., "...")   : 그 폴더 아래 "linkbio_data" 폴더
# 이렇게 하면 스크립트를 어디서 실행하든 항상 스크립트 옆에 저장된다.
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "linkbio_data")

# User-Agent: "나는 이런 브라우저야"라고 서버에 알려 주는 문자열.
# 이 값을 안 보내면 일부 사이트가 "봇이네" 하고 다른(혹은 빈) 페이지를 주기도 해서,
# 실제 크롬 브라우저인 것처럼 위장해 둔다.
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
HEADERS = {"User-Agent": UA}  # 모든 requests 요청에 함께 보낼 헤더


def normalize_image_url(image: str | None) -> str | None:
    """litt.ly productLink 이미지는 //domain/... 형태(스킴 없음)로 오는 경우가 있어 https:// 보정.

    [설명]
    이미지 주소가 가끔 "https:"가 빠진 "//example.com/a.jpg" 형태로 온다.
    이건 "현재 페이지와 같은 프로토콜을 써라"는 웹 관례인데, 우리가 나중에
    이 주소를 그냥 쓰려면 앞에 "https:"를 붙여 완전한 주소로 만들어 줘야 한다.
      - image가 None이거나 빈 값이면 그대로 None 반환
      - "//"로 시작하면 앞에 "https:"를 붙임
      - 아니면(이미 온전한 주소면) 그대로 반환
    """
    if not image:
        return None
    return f"https:{image}" if image.startswith("//") else image


def detect_platform(url: str) -> str:
    """URL의 도메인(host)만 보고 어느 링크 서비스인지 이름을 판별한다.

    [설명]
    urlparse("https://litt.ly/abc").hostname  →  "litt.ly"  처럼
    주소에서 도메인 부분만 뽑아, 미리 아는 도메인 목록과 하나씩 비교한다.
    목록에 없는 도메인이면 우리가 처리할 수 없으므로 에러(ValueError)를 낸다.
    """
    host = (
        urlparse(url).hostname or ""
    )  # hostname이 없으면(비정상 URL) 빈 문자열로 처리
    if host == "litt.ly":
        return "littly"
    if host in (
        "link.inpock.co.kr",
        "inpk.link",
    ):  # inpk.link는 인포크 축약형(동일 구조)
        return "inpock"
    if host in ("linktr.ee", "tr.ee"):  # tr.ee는 링크트리 축약형(동일 구조)
        return "linktree"
    if host == "hity.io":
        return "hity"
    if host == "instabio.cc":
        return "instabio"
    if host == "bio.site":
        return "biosite"
    if host == "linkon.id":
        return "linkon"
    if host == "linkseller.net":
        return "linkseller"
    raise ValueError(f"unsupported host: {host}")  # 모르는 도메인 → 에러


def extract_balanced_json(html: str, marker: str) -> str | None:
    """window.__data= 같은 JS 할당문 뒤에 이어지는 balanced JSON({...} 또는 [...])을 문자열로 추출.

    [왜 이런 함수가 필요한가]
    instabio, bio.site 같은 곳은 데이터가 <script> 태그가 아니라 자바스크립트
    코드 한복판에  window.__data = { ...아주 긴 JSON... };  형태로 들어 있다.
    JSON 뒤에 또 다른 코드가 붙어 있어서, 정규식만으로 "여기서 JSON이 끝난다"를
    정확히 잡기 어렵다. (JSON 안에도 { } 가 잔뜩 있기 때문)
    그래서 여는 괄호와 닫는 괄호의 개수를 직접 세어 가며, 짝이 딱 맞는(balanced)
    지점을 찾아 JSON 부분만 정확히 잘라낸다.

    [동작 원리]
    1) marker(예: "window.__data")가 나오는 위치를 찾는다.
    2) 그 뒤의 '=' 다음에서 공백을 건너뛰고, 첫 '{' 또는 '[' 를 JSON 시작점으로 잡는다.
    3) 문자를 하나씩 보며 여는 괄호는 depth+1, 닫는 괄호는 depth-1.
       depth가 다시 0이 되는 순간이 JSON의 끝이다.
    4) 단, 문자열("...") 안에 있는 괄호는 세면 안 되므로 in_str 플래그로 구분하고,
       역슬래시로 이스케이프된 따옴표(\\")는 문자열의 끝이 아니므로 esc로 처리한다.
    """
    idx = html.find(marker)  # marker 위치 찾기 (없으면 -1)
    if idx == -1:
        return None
    i = html.index("=", idx) + 1  # marker 뒤의 첫 '=' 다음 위치로 이동
    while i < len(html) and html[i].isspace():  # '=' 뒤 공백 건너뛰기
        i += 1
    if i >= len(html) or html[i] not in "{[":  # JSON 시작({ 또는 [)이 아니면 실패
        return None
    open_ch = html[i]  # 여는 괄호 ( '{' 또는 '[' )
    close_ch = "}" if open_ch == "{" else "]"  # 그에 대응하는 닫는 괄호
    depth = 0  # 괄호 중첩 깊이
    in_str = False  # 지금 문자열("...") 안에 있는가?
    esc = False  # 직전 문자가 이스케이프용 역슬래시였나?
    start = i  # JSON이 시작되는 위치 기억
    while i < len(html):
        c = html[i]
        if in_str:  # ── 문자열 내부일 때 ──
            if esc:  # 이스케이프된 문자면 그냥 넘김
                esc = False
            elif c == "\\":  # 역슬래시 → 다음 문자는 이스케이프됨
                esc = True
            elif c == '"':  # 따옴표 → 문자열 끝
                in_str = False
        else:  # ── 문자열 바깥(진짜 구조)일 때 ──
            if c == '"':  # 따옴표 → 문자열 시작
                in_str = True
            elif c == open_ch:  # 여는 괄호 → 깊이 증가
                depth += 1
            elif c == close_ch:  # 닫는 괄호 → 깊이 감소
                depth -= 1
                if depth == 0:  # 깊이가 0이 되면 짝이 딱 맞은 것 → JSON 끝
                    return html[start : i + 1]
        i += 1
    return None  # 끝까지 짝이 안 맞으면(비정상) None


def extract_raw(platform: str, html: str) -> dict:
    """플랫폼별로 페이지에 embed된 원본 데이터(JSON)를 추출.

    [설명]
    HTML은 그냥 긴 문자열이다. 그 안에서 우리가 원하는 JSON 조각을 찾는 방법이
    플랫폼마다 다르기 때문에, if 문으로 플랫폼을 나눠 각기 다른 방식으로 꺼낸다.
    반환값은 "아직 우리가 정리하지 않은, 서비스가 준 날것 그대로의 dict"이다.

    * 정규식 re.search(패턴, html, re.S)에서 re.S는 '.'가 줄바꿈까지 포함하게 하는 옵션.
      JSON이 여러 줄에 걸쳐 있어도 통째로 잡기 위해 꼭 필요하다.
    * (.*?)의 '?'는 '최소한만 매칭'(non-greedy) — </script>가 처음 나오는 곳까지만 잡는다.
    """
    if platform == "littly":
        # litt.ly: <script id="data">안의 내용이 base64로 인코딩되어 있어 디코딩 후 JSON 파싱.
        m = re.search(r'<script id="data" type="text/plain">(.*?)</script>', html, re.S)
        if not m:
            raise ValueError("litt.ly: #data script tag not found")
        return json.loads(base64.b64decode(m.group(1)))  # base64 디코딩 → JSON 파싱

    if platform in ("inpock", "linktree", "hity"):
        # Next.js 기반 사이트들은 __NEXT_DATA__ 스크립트에 페이지 데이터를 담아 둔다.
        # 실제 우리가 원하는 알맹이는 props.pageProps 안에 있다.
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
        if not m:
            raise ValueError(f"{platform}: __NEXT_DATA__ script tag not found")
        return json.loads(m.group(1))["props"]["pageProps"]

    if platform == "instabio":
        # 자바스크립트 코드 속 window.__data = {...} 형태 → 위의 균형 괄호 파서 사용
        raw = extract_balanced_json(html, "window.__data")
        if not raw:
            raise ValueError("instabio: window.__data not found")
        return json.loads(raw)

    if platform == "biosite":
        # 마찬가지로 window.initial_state = {...} 형태
        raw = extract_balanced_json(html, "window.initial_state")
        if not raw:
            raise ValueError("biosite: window.initial_state not found")
        return json.loads(raw)

    if platform in ("linkon", "linkseller"):
        # 같은 화이트라벨 솔루션. 링크 목록이 hidden input(jsonLinkList)에 escape된 JSON으로 embed됨.
        #   (화이트라벨 = 같은 프로그램을 이름/도메인만 바꿔 여러 곳에 파는 것 → 구조가 동일)
        # value="..." 안의 문자열은 HTML escape되어 있어(html_lib.unescape) 복원 후 JSON 파싱.
        m = re.search(r'id="jsonLinkList"[^>]*value="([^"]*)"', html)
        if not m:
            raise ValueError(f"{platform}: jsonLinkList input not found")
        link_list = json.loads(html_lib.unescape(m.group(1)))
        # 프로필 제목/설명은 별도 JSON이 없어 <meta property="og:..."> 태그에서 가져온다.
        og_title = re.search(r'property="og:title" content="([^"]*)"', html)
        og_desc = re.search(r'property="og:description" content="([^"]*)"', html)
        return {
            "linkList": link_list,
            "title": html_lib.unescape(og_title.group(1)) if og_title else None,
            "description": html_lib.unescape(og_desc.group(1)) if og_desc else None,
        }

    raise ValueError(f"unsupported platform: {platform}")


def get_username(url: str) -> str:
    """URL 경로에서 계정명(username)을 뽑아낸다.

    [설명]
    예: "https://litt.ly/hello/world" → path는 "/hello/world"
       .strip("/") 로 앞뒤 슬래시 제거 → "hello/world"
       .split("/")[0] 로 첫 조각만 → "hello"  (이게 계정명)
    """
    path = urlparse(url).path.strip("/")
    if not path:
        raise ValueError(f"cannot extract username from url: {url}")
    return path.split("/")[0]


def fetch_raw(url: str) -> dict:
    """가공하지 않은 원본 데이터(JSON) 그대로 반환. 플랫폼별 embed 위치가 달라 extract_raw로 위임.

    [언제 쓰나]
    나중에 "우리가 정리한 결과가 이상한데?" 싶을 때 원본과 대조하려고 남겨 두는 용도.
    run_batch(save_raw=True) 일 때만 호출되어 raw/ 폴더에 함께 저장된다.
    """
    platform = detect_platform(url)
    res = requests.get(url, headers=HEADERS, timeout=10)  # HTML 다운로드
    res.raise_for_status()  # 404/500 등이면 예외 발생
    raw = extract_raw(platform, res.text)  # HTML에서 원본 JSON 꺼내기
    return {
        "platform": platform,
        "source_url": url,
        "username": get_username(url),
        "raw": raw,
    }


def resolve_final_url(url: str) -> str | None:
    """srok.kr, pf.kakao.com 등 대부분 단축/중계 링크이므로 최종 목적지까지 follow.

    [설명]
    링크 모음 페이지의 버튼은 대개 "단축 URL"이나 "클릭 추적용 중계 URL"이다.
    이걸 그대로 저장하면 진짜 어디로 가는지 모른다. 그래서 실제로 한 번 접속해서
    (allow_redirects=True) 리다이렉트를 끝까지 따라간 뒤, 최종 도착 주소(res.url)를
    돌려준다. 접속이 실패하면 프로그램을 멈추지 않고 None을 돌려준다(예외 무시).
    """
    if not url:
        return None
    try:
        res = requests.get(url, headers=HEADERS, allow_redirects=True, timeout=10)
        return res.url
    except requests.RequestException:
        return None


# ============================================================================
# 여기서부터는 플랫폼별 파서.
# 모든 parse_* 함수는 같은 골격을 따른다:
#   1) HTML 다운로드 → 2) 원본 JSON 추출 → 3) 링크/상품 목록 정리
#   → 4) (원하면) 단축링크를 병렬로 최종 주소까지 추적 → 5) 공통 형태 dict 반환
# resolve_links=False로 부르면 4)단계를 건너뛴다(빠르지만 resolved_url이 None).
# ============================================================================


# ---------- litt.ly ----------


def parse_littly(url: str, resolve_links: bool = True) -> dict:
    res = requests.get(url, headers=HEADERS, timeout=10)
    res.raise_for_status()

    # HTML에서 <script id="data"> 내용(=base64 JSON) 찾기
    m = re.search(r'<script id="data" type="text/plain">(.*?)</script>', res.text, re.S)
    if not m:
        # 페이지 구조가 바뀌면 여기서 걸린다 → 정규식/파싱 로직을 갱신해야 한다는 신호
        raise ValueError(
            "litt.ly: #data script tag not found (page structure may have changed)"
        )

    data = json.loads(base64.b64decode(m.group(1)))  # base64 → JSON
    theme = data.get("theme") or {}  # 테마 정보 (없으면 빈 dict)
    blocks = data.get("blocks") or []  # 페이지를 구성하는 블록 목록

    # 블록에는 여러 종류(type)가 있다. 우리가 원하는 건 일반 링크와 상품 링크.
    links = [b for b in blocks if b.get("type") == "link"]
    product_link_blocks = [b for b in blocks if b.get("type") == "productLink"]

    # 일반 링크들의 진짜 목적지를 병렬로 추적.
    # ThreadPoolExecutor(max_workers=8): 최대 8개를 동시에 처리해 속도를 높인다.
    #   pool.map(함수, 리스트): 리스트의 각 요소에 함수를 적용하되 여러 개를 동시에 실행.
    if resolve_links and links:
        with ThreadPoolExecutor(max_workers=8) as pool:
            resolved = list(pool.map(lambda b: resolve_final_url(b.get("url")), links))
    else:
        resolved = [None] * len(links)  # 추적 안 할 땐 전부 None으로 채움

    # productLink 블록은 title/price/originalPrice/image/url을 이미 다 담고 있어 리다이렉트 resolve가 필요 없음
    # (이중 for: 상품 블록 여러 개 → 각 블록 안의 상품 여러 개를 펼쳐서 하나의 리스트로)
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
        # zip(links, resolved): 링크와 그 링크의 최종주소를 짝지어 함께 순회
        "links": [
            {
                "title": b.get("title"),
                "url": b.get("url"),
                "resolved_url": real_url,
                # image가 dict면 그 안의 "url", 아니면 image 값 자체를 사용(데이터 형태가 섞여 있음)
                "image": b["image"]["url"]
                if isinstance(b.get("image"), dict)
                else b.get("image"),
                "folded": b.get("folded"),  # 접힘 여부
                "emphasized": b.get("emphasized"),  # 강조 여부
            }
            for b, real_url in zip(links, resolved)
        ],
        "products": products,
    }


# ---------- inpock ----------


def parse_inpock(url: str, resolve_links: bool = True) -> dict:
    # 도메인이 여러 개(link.inpock.co.kr, inpk.link)라서, 원본 URL에서
    # scheme+host를 그대로 뽑아 base 주소로 삼는다(단축링크 추적 시 이 base를 붙임).
    base = f"{urlparse(url).scheme}://{urlparse(url).hostname}"
    username = get_username(url)

    res = requests.get(f"{base}/{username}", headers=HEADERS, timeout=10)
    res.raise_for_status()

    # Next.js __NEXT_DATA__ 스크립트에서 JSON 추출
    m = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        res.text,
        re.S,
    )
    if not m:
        raise ValueError(
            "inpock: __NEXT_DATA__ script tag not found (page structure may have changed)"
        )

    page_props = json.loads(m.group(1))["props"]["pageProps"]
    design = page_props.get("design") or {}  # 프로필 디자인/텍스트 정보
    blocks = page_props.get("blocks") or []  # 페이지 블록 목록

    # 블록을 종류(block_type)별로 분류
    link_blocks = [b for b in blocks if b.get("block_type") == "link"]  # 일반 링크
    text_blocks = [b for b in blocks if b.get("block_type") == "text"]  # 텍스트
    store_blocks = [
        b for b in blocks if b.get("block_type") == "smart_store"
    ]  # 네이버 스마트스토어
    collection_blocks = [
        b for b in blocks if b.get("block_type") == "collection"
    ]  # 상품 모음
    # divider 등 나머지 block_type은 표시용일 뿐 데이터가 없어 무시

    def resolve(path):
        """inpock 내부 단축경로(/api/r/...)만 최종 주소로 추적. 그 외 값은 그대로 둔다."""
        if not path or not path.startswith("/api/r/"):
            return path
        try:
            r = requests.get(
                f"{base}{path}", headers=HEADERS, allow_redirects=True, timeout=10
            )
            return r.url
        except requests.RequestException:
            return None

    # link/smart_store/collection 블록의 url과 그 안의 상품 url까지 한 번에 병렬 resolve
    # [핵심 트릭] 추적할 URL을 "한 리스트(resolve_targets)"에 정해진 순서대로 다 모은 뒤
    #            한 번에 병렬 처리하고, 그 결과를 다시 원래 자리로 나눠 담는다.
    #            (요청을 잘게 여러 번 하는 것보다 한 방에 몰아 하는 게 훨씬 빠르다)
    resolve_targets = [b.get("url") for b in link_blocks]  # ① 링크 블록의 url
    resolve_targets += [b.get("url") for b in store_blocks]  # ② 스토어 블록의 url
    for b in store_blocks:  # ③ 스토어 안 상품들의 url
        resolve_targets += [p.get("url") for p in b.get("products", [])]
    for b in collection_blocks:  # ④ 컬렉션 안 상품들의 url
        resolve_targets += [p.get("url") for p in b.get("links", [])]

    if resolve_links and resolve_targets:
        with ThreadPoolExecutor(max_workers=8) as pool:
            resolved = list(pool.map(resolve, resolve_targets))
    else:
        resolved = [None] * len(resolve_targets)

    # [결과 되돌려 담기] resolved는 위 ①②③④ 순서로 일렬로 붙어 있다.
    # iter()로 "다음 값을 하나씩 꺼내 주는 커서"를 만들어, 넣은 순서 그대로 다시 빼낸다.
    # next(it)를 부를 때마다 resolved의 다음 값이 나온다 → 순서가 어긋나지 않는 게 핵심.
    it = iter(resolved)
    link_resolved = [next(it) for _ in link_blocks]  # ① 되찾기
    store_resolved = [next(it) for _ in store_blocks]  # ② 되찾기
    store_product_resolved = {  # ③ 스토어id별 상품주소들
        b["id"]: [next(it) for _ in b.get("products", [])] for b in store_blocks
    }
    collection_item_resolved = {  # ④ 컬렉션id별 상품주소들
        b["id"]: [next(it) for _ in b.get("links", [])] for b in collection_blocks
    }

    # 스마트스토어 블록 → 우리 형태로 정리 (블록 안에 상품 목록이 중첩됨)
    smart_stores = [
        {
            "title": b.get("title"),
            "url": b.get("url"),
            "resolved_url": real_url,
            "is_open": b.get("is_open"),  # 공개 여부
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
                for p, product_url in zip(
                    b.get("products", []), store_product_resolved[b["id"]]
                )
            ],
        }
        for b, real_url in zip(store_blocks, store_resolved)
    ]

    # 컬렉션(상품 모음) 블록 → 우리 형태로 정리
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
                for p, item_url in zip(
                    b.get("links", []), collection_item_resolved[b["id"]]
                )
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
        "notice": (design.get("notice") or {}).get("contents"),  # 공지사항 본문
        "background_color": design.get("background_color"),
        "sns": design.get("sns"),  # SNS 계정 정보
        "links": [
            {
                "title": b.get("title"),
                "url": b.get("url"),
                "resolved_url": real_url,
                "image": b.get("image"),
                "stickers": [
                    s.get("title") for s in b.get("stickers", [])
                ],  # "NEW" 등 뱃지
                "is_open": b.get("is_open"),
            }
            for b, real_url in zip(link_blocks, link_resolved)
        ],
        "texts": [
            b.get("title") for b in text_blocks if b.get("is_open")
        ],  # 공개된 텍스트만
        "smart_stores": smart_stores,
        "collections": collections,
    }


# ---------- linktree ----------


def parse_linktree(url: str, resolve_links: bool = True) -> dict:
    res = requests.get(url, headers=HEADERS, timeout=10)
    res.raise_for_status()

    m = re.search(
        r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>',
        res.text,
        re.S,
    )
    if not m:
        raise ValueError(
            "linktree: __NEXT_DATA__ script tag not found (page structure may have changed)"
        )

    page_props = json.loads(m.group(1))["props"]["pageProps"]

    # HEADER 타입은 섹션 구분용 텍스트일 뿐 url이 없어 제외
    link_blocks = [
        b
        for b in (page_props.get("links") or [])
        if b.get("type") != "HEADER" and b.get("url")
    ]

    if resolve_links and link_blocks:
        with ThreadPoolExecutor(max_workers=8) as pool:
            resolved = list(
                pool.map(lambda b: resolve_final_url(b.get("url")), link_blocks)
            )
    else:
        resolved = [None] * len(link_blocks)

    # 상품 판매형(commerceStorefrontItems)은 샘플 계정에 데이터가 없어 필드 구조 미검증 — 존재 시 방어적으로만 추출
    #   (.get("title") or .get("name") 처럼 여러 후보 키를 or로 시도하는 게 방어적 추출)
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


# ---------- hity ----------


def parse_hity(url: str, resolve_links: bool = True) -> dict:
    res = requests.get(url, headers=HEADERS, timeout=10)
    res.raise_for_status()
    page_props = extract_raw("hity", res.text)  # __NEXT_DATA__.props.pageProps
    space = page_props.get("space") or {}  # hity는 페이지를 "space"라고 부른다
    sections = space.get("sections") or []  # space는 여러 섹션으로 구성

    # 각 섹션의 링크 정보(spaceSectionLinkInfos)를 flatten. link.target이 실제 목적지.
    # [flatten이란] 섹션 > 링크정보 처럼 2겹으로 중첩된 구조를 1차원 리스트로 펼치는 것.
    raw_links = []
    for sec in sections:
        for info in sec.get("spaceSectionLinkInfos") or []:
            link = info.get("link") or {}
            raw_links.append(
                {
                    "title": info.get("title") or info.get("description"),
                    "url": link.get("target"),
                    "image": info.get("imageUrl"),
                    "section_type": sec.get("type"),
                }
            )

    raw_links = [l for l in raw_links if l["url"]]  # url이 없는 항목은 버림

    if resolve_links and raw_links:
        with ThreadPoolExecutor(max_workers=8) as pool:
            resolved = list(pool.map(lambda l: resolve_final_url(l["url"]), raw_links))
    else:
        resolved = [None] * len(raw_links)

    # 상품 판매(spaceSectionShopInfos)는 샘플 계정에 데이터가 없어 구조 미검증 — 존재 시 방어적으로만 추출
    products = []
    for sec in sections:
        for shop in sec.get("spaceSectionShopInfos") or []:
            products.append(
                {
                    "title": shop.get("title") or shop.get("name"),
                    "price": shop.get("price") or shop.get("salePrice"),
                    "image": shop.get("imageUrl") or shop.get("image"),
                    # link이 dict면 그 안의 target, 아니면 link 값 자체(형태가 섞여 있어 방어적으로)
                    "url": (shop.get("link") or {}).get("target")
                    if isinstance(shop.get("link"), dict)
                    else shop.get("link"),
                }
            )

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


# ---------- instabio ----------


def parse_instabio(url: str, resolve_links: bool = True) -> dict:
    res = requests.get(url, headers=HEADERS, timeout=10)
    res.raise_for_status()
    data = extract_raw("instabio", res.text)  # window.__data 안의 JSON
    ui = data.get("ui") or {}  # 프로필 표시 정보(이름/소개 등)
    cmpts = (data.get("content") or {}).get("cmpts") or []  # 컴포넌트(버튼 등) 목록

    # 버튼형 컴포넌트(links[])의 각 링크를 flatten. cmpt 자체 단일 link도 포함.
    raw_links = []
    for c in cmpts:
        for l in c.get("links") or []:  # 컴포넌트 안에 링크가 여러 개인 경우
            if l.get("state") == 0:  # state==0 은 비활성/숨김 → 건너뜀
                continue
            raw_links.append(
                {
                    "title": l.get("title"),
                    "url": l.get("link")
                    or l.get("link1"),  # 키 이름이 두 가지라 or로 처리
                    "image": l.get("icon") or c.get("image"),
                }
            )
        # 링크 배열이 없고 컴포넌트 자체에 단일 link가 있는 경우도 챙긴다
        if not c.get("links") and c.get("link"):
            raw_links.append(
                {"title": c.get("title"), "url": c.get("link"), "image": c.get("image")}
            )

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
                "image": normalize_image_url(l["image"]),  # 스킴 없는 이미지 주소 보정
            }
            for l, real_url in zip(raw_links, resolved)
        ],
        "products": [],
    }


# ---------- bio.site ----------


def parse_biosite(url: str, resolve_links: bool = True) -> dict:
    res = requests.get(url, headers=HEADERS, timeout=10)
    res.raise_for_status()
    data = extract_raw("biosite", res.text)  # window.initial_state 안의 JSON
    header = data.get("header") or {}  # 프로필 헤더(이름/소개)
    body = data.get("body") or []  # 본문 섹션들

    # 본문 섹션 중 type이 "section_links"인 것에서 링크를 모은다
    raw_links = []
    for sec in body:
        if sec.get("type") == "section_links":
            for l in (sec.get("section") or {}).get("links") or []:
                raw_links.append(
                    {
                        "title": l.get("name") or l.get("title"),
                        "url": l.get("url"),
                        "image": l.get("image") or l.get("thumbnail"),
                    }
                )

    raw_links = [l for l in raw_links if l["url"]]

    if resolve_links and raw_links:
        with ThreadPoolExecutor(max_workers=8) as pool:
            resolved = list(pool.map(lambda l: resolve_final_url(l["url"]), raw_links))
    else:
        resolved = [None] * len(raw_links)

    # 소셜 핸들 — "section_social" 섹션을 찾으면 그 안의 handles를 쓰고 반복 종료(break)
    sns = []
    for sec in body:
        if sec.get("type") == "section_social":
            sns = (sec.get("section") or {}).get("handles")
            break

    return {
        "platform": "biosite",
        "source_url": url,
        # 계정명은 메타데이터의 handle을 우선 쓰고, 없으면 URL에서 뽑는다
        "username": (data.get("metadata") or {}).get("handle") or get_username(url),
        "title": header.get("name"),
        "bio": header.get("bio"),
        "background_color": None,
        "sns": sns,
        "links": [
            {
                "title": l["title"],
                "url": l["url"],
                "resolved_url": real_url,
                "image": l["image"],
            }
            for l, real_url in zip(raw_links, resolved)
        ],
        "products": [],
    }


# ---------- linkon / linkseller (동일 화이트라벨 솔루션) ----------


def parse_linktool(url: str, resolve_links: bool = True) -> dict:
    # linkon과 linkseller는 같은 프로그램을 도메인만 바꿔 파는 것이라 처리가 동일하다.
    # 그래서 파서 하나(parse_linktool)로 둘 다 담당한다.
    platform = detect_platform(url)
    base = f"{urlparse(url).scheme}://{urlparse(url).hostname}"
    username = get_username(url)

    res = requests.get(url, headers=HEADERS, timeout=10)
    res.raise_for_status()
    raw = extract_raw(platform, res.text)  # hidden input에서 꺼낸 링크 목록 등

    # boxtype이 link인 항목만 (text/schedule/ad/cslink 등은 링크 아님)
    link_items = [
        b for b in raw["linkList"] if b.get("boxtype") == "link" and b.get("lpl_url")
    ]

    if resolve_links and link_items:
        with ThreadPoolExecutor(max_workers=8) as pool:
            resolved = list(
                pool.map(lambda b: resolve_final_url(b.get("lpl_url")), link_items)
            )
    else:
        resolved = [None] * len(link_items)

    def image_url(fname):
        """썸네일 파일명(ll_img)만 있고 전체 주소는 없어서, 규칙에 맞춰 주소를 조립한다."""
        # 렌더링된 img src가 ico/{username}/thum2/{파일명} 형태
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
                "title": b.get("ll_name"),  # 링크 이름 (이 서비스는 컬럼명이 ll_name)
                "url": b.get("lpl_url"),  # 링크 주소 (컬럼명이 lpl_url)
                "resolved_url": real_url,
                "image": image_url(b.get("ll_img")),
            }
            for b, real_url in zip(link_items, resolved)
        ],
        "products": [],
    }


# 플랫폼 이름 → 그 플랫폼을 처리하는 함수 를 연결한 표(dispatch table).
# parse()에서 이 표를 보고 알맞은 함수를 골라 부른다. (긴 if-elif 없이 깔끔하게)
PARSERS = {
    "littly": parse_littly,
    "inpock": parse_inpock,
    "linktree": parse_linktree,
    "hity": parse_hity,
    "instabio": parse_instabio,
    "biosite": parse_biosite,
    "linkon": parse_linktool,  # linkon/linkseller는 같은 함수를 공유
    "linkseller": parse_linktool,
}


def parse(url: str, **kwargs) -> dict:
    """url값(사용자명, 리다이렉트 토큰 등)은 계속 바뀌므로 항상 이 함수로 실시간 조회할 것 — 결과를 캐싱해서 재사용하지 말 것.

    [이 함수가 대표 진입점]
    바깥에서는 이 parse(url) 하나만 부르면 된다. 안에서 플랫폼을 판별하고
    알맞은 parse_* 함수를 대신 불러 준다.
    **kwargs 는 resolve_links 같은 추가 옵션을 그대로 넘겨 주는 통로다.
    """
    platform = detect_platform(url)
    return PARSERS[platform](url, **kwargs)


def save(data: dict, out_dir: str = OUTPUT_DIR, suffix: str = "") -> str:
    """결과 dict를 "플랫폼_계정명.json" 파일로 저장하고, 저장 경로를 돌려준다."""
    os.makedirs(out_dir, exist_ok=True)  # 폴더 없으면 만들고, 있으면 그냥 통과
    out_path = os.path.join(
        out_dir, f"{data['platform']}_{data['username']}{suffix}.json"
    )
    with open(out_path, "w", encoding="utf-8") as f:
        # ensure_ascii=False : 한글이 \uXXXX로 깨지지 않고 그대로 저장되게
        # indent=2           : 보기 좋게 2칸 들여쓰기
        json.dump(data, f, ensure_ascii=False, indent=2)
    return out_path


def collect_urls(args: list[str]) -> list[str]:
    """.txt 파일 경로가 주어지면 줄 단위로 읽고, 아니면 인자 자체를 URL 목록으로 취급.

    [사용 예]
    python parse_linkbio.py urls.txt          → urls.txt 파일을 한 줄씩 URL로 읽음
    python parse_linkbio.py https://a https://b → 인자 두 개를 그대로 URL 목록으로
    (txt 파일에서 빈 줄과 #으로 시작하는 주석 줄은 무시한다)
    """
    if len(args) == 1 and args[0].endswith(".txt") and os.path.isfile(args[0]):
        with open(args[0], encoding="utf-8") as f:
            return [
                line.strip() for line in f if line.strip() and not line.startswith("#")
            ]
    return args


def run_batch(
    urls: list[str],
    delay_range: tuple[float, float] = (1.5, 3.5),
    save_raw: bool = False,
) -> None:
    """save_raw=True면 가공된 결과와 별개로 원본(raw) JSON도 linkbio_data/raw/ 에 저장.

    [이 함수가 실제 일괄 실행기]
    URL 목록을 하나씩 돌면서 parse → save 하고, 성공/실패를 집계해 마지막에 요약 출력.
    각 URL 사이에는 delay_range 범위의 무작위 시간만큼 쉰다 → 너무 빠른 연속 요청으로
    상대 서버에 부담(또는 차단)을 주지 않기 위한 예의이자 안전장치.
    """
    ok, failed = [], []  # 성공/실패한 URL을 담을 리스트

    for i, url in enumerate(urls, 1):  # enumerate(..., 1): 1번부터 번호 매김
        print(f"[{i}/{len(urls)}] {url}")
        try:
            data = parse(url)  # 파싱 시도
            out_path = save(data)  # 결과 저장
            ok.append(url)
            print(f"  -> saved: {out_path} (links: {len(data['links'])})")

            if save_raw:  # 원본도 함께 저장하는 옵션
                raw_path = save(fetch_raw(url), out_dir=os.path.join(OUTPUT_DIR, "raw"))
                print(f"  -> raw saved: {raw_path}")
        except Exception as e:
            # 한 URL이 실패해도 전체가 멈추지 않도록 예외를 잡아 기록만 하고 계속 진행
            failed.append((url, str(e)))
            print(f"  -> FAILED: {e}")

        if i < len(urls):  # 마지막 URL 뒤에는 쉴 필요 없음
            time.sleep(random.uniform(*delay_range))  # 무작위로 잠깐 대기

    # 전체 결과 요약
    print(f"\ndone: {len(ok)} succeeded, {len(failed)} failed")
    for url, err in failed:
        print(f"  - {url}: {err}")  # 실패한 것들은 이유와 함께 다시 나열


# 테스트용: 여기 URL을 직접 추가/삭제하면서 테스트. 나중에 안정화되면 CSV/MySQL 등에서 불러오도록 교체.
TEST_URLS = [
    "https://link.inpock.co.kr/181213_hy",
]

# True로 바꾸면 가공된 결과와 별개로 원본 JSON도 linkbio_data/raw/ 에 저장.
SAVE_RAW = True


# 이 파일을 직접 실행했을 때만(=import 되었을 땐 말고) 아래가 동작한다.
#   python parse_linkbio.py          → 인자가 없으니 TEST_URLS 사용
#   python parse_linkbio.py urls.txt → 커맨드라인 인자를 URL 목록으로 사용
if __name__ == "__main__":
    urls = collect_urls(sys.argv[1:]) if len(sys.argv) > 1 else TEST_URLS
    run_batch(urls, save_raw=SAVE_RAW)
