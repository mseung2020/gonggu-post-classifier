"""
================================================================================
 링크인바이오(link-in-bio) 페이지 파서
================================================================================

인스타그램 프로필 등에 걸려 있는 "링크 모음 페이지" 주소(URL)를 받아서, 그 안에 들어 있는
링크/상품/프로필 정보를 깔끔한 JSON 형태로 뽑아낸다.

[배경 지식: 링크인바이오란?]
  인스타그램은 게시물 본문에 클릭 가능한 링크를 못 넣는다. 그래서 사람들은 litt.ly,
  링크트리(linktr.ee), 인포크(inpock) 같은 서비스에서 "링크 모음 페이지"를 하나 만들어
  두고, 프로필에 그 주소 하나만 걸어 둔다.

[핵심 아이디어 — 왜 이렇게 만들었나]
  이런 페이지들은 대부분 "서버가 미리 만든 HTML"을 내려주는데, 그 HTML 안 어딘가에
  페이지를 그리는 데 쓰인 원본 데이터(JSON)가 통째로 박혀 있다. 따라서 브라우저를 흉내
  낼 필요 없이 HTML을 그냥 받아서(requests) 그 안에 박힌 JSON만 골라 꺼내면 된다.

[패키지 구성 — 책임별로 분리]
  hosts.py      : URL 도메인 -> 플랫폼 이름 판별(detect_platform)
  extract.py    : 플랫폼별로 HTML에 embed된 원본 JSON 위치를 찾아 꺼내기
  common.py     : 공통 HTTP 헤더/계정명 추출/단축링크 추적 등 저수준 헬퍼
  platforms/    : 플랫폼별 파서(전략 패턴) — 각각 parse(url, resolve_links) -> dict
  registry.py   : 플랫폼 이름 -> 파서 dispatch table + 공개 진입점(parse/fetch_raw/save)
  batch.py      : URL 목록 일괄 처리(수동 테스트/진단용, 메인 파이프라인은 안 씀)

[공개 API — 이 패키지 밖에서는 이것만 쓰면 됨]
  detect_platform(url) -> str        # 지원 안 하는 도메인이면 ValueError
  parse(url, resolve_links=True) -> dict
  fetch_raw(url) -> dict             # 가공 전 원본 데이터(진단용)
  save(data, out_dir=..., suffix=...) -> str

[결과 dict의 공통 형태]
  {
    "platform": "inpock", "source_url": "...", "username": "...",
    "title": "...", "bio": "...",
    "links": [ {title, url, resolved_url, image, ...}, ... ],
    "products": [ ... ],   # (일부 플랫폼만)
  }
================================================================================
"""
from .hosts import detect_platform
from .registry import PARSERS, fetch_raw, parse, save

__all__ = ["detect_platform", "parse", "fetch_raw", "save", "PARSERS"]
