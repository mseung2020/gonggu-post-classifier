"""4.5단계(transform.py와 load.py 사이): load_ready.json의 각 상품이 가진 candidate_url
(LLM#1이 캡션/프로필에서 뽑은 원본 후보 링크들, 세미콜론으로 이어붙여진 상태)을 실제로
크롤링해서 "찐 최종 링크 하나"로 좁힌다.

흐름 (post -> 프로필/링크모음 -> 상품. 옛 gonggu-link-resolver/scripts/resolver.py의
크롤링 로직을 이 프로젝트의 상품 배열 스키마에 맞게 이식):
  candidate_url의 후보(세미콜론 구분) 중 (핸들-슬러그 일치 > 링크인바이오 허브 > 확정몰 >
  그 외) 순으로 정렬(ranking.rank_candidates) 후 하나씩 시도 — 상품과 무관한 게 명백한
  도메인(forms.gle 등)은 이 단계에서 아예 제거된다. 하나가 실패하면 다음 후보로 넘어가고,
  하나라도 done이 나오면 즉시 확정.
  후보 하나에 대한 시도(core._resolve_one_candidate):
    -> LLM#3(페이지판별): 도착한 페이지가 원본 포스트 상품의 "최종 상품페이지"인지 판별
       - 최종 상품페이지로 확정 -> candidate_url을 이 URL로 교체, done
       - 링크모음/스토어메인이면: 페이지의 링크 후보 추출(스크립트) -> LLM#2(링크선택)로 하나
         고름 -> 확신도(confidence)가 충분하면(LINK_PICK_OK_CONF) ⚠ 그 링크를 실제로 열어서
         재검증하지 않고 즉시 최종 후보로 확정, done (네이버 등 최종 목적지에서 자주 걸리는
         안티봇 차단을 원천적으로 피하기 위함 — 그 대신 판단의 무게중심을 LLM#2 쪽으로 옮겨서
         링크선택 프롬프트를 더 신중하게 다듬어둠)
       - 로그인월_차단/무관/확신도 낮은 링크선택 등 -> 이 후보는 실패, 다음 후보로
  모든 후보가 실패하면 그중 가장 나은 상태(hold > unresolved > error)를 반환한다. candidate_url은
  상태와 무관하게 항상 대표 URL 1개다(2026-07-29 결정, DB에 세미콜론 구분 원본 목록을 절대
  남기지 않음) — hold면 실제로 열어봤던 페이지, 그마저 없으면 정렬 1순위 후보(대부분 링크인바이오
  허브). 실제로 시도한 URL들은 결과의 tried_urls에 남아서 나중에 진단 가능.

⚠ 마감/예정 등 진행 단계와 무관하게 항상 해석을 시도한다 — 공구가 끝났거나 아직 안 열렸어도
프로필의 링크모음(인포크 등)에 상품 링크가 걸려있을 수 있으므로 미리 걸러내지 않음.

⚠ 이 단계는 "링크를 하나로 확정"까지만 담당한다. 그 링크를 열어서 실제 가격/이미지/옵션 등
진짜 상품 데이터를 가져오는 것은 이 파이프라인 밖(다른 개발자 담당)이다.

DeepSeek API 키 필요(.env): DEEPSEEK_KEY (LLM#2 "공구왕 링크선택", LLM#3 "공구왕 페이지판별" 공용)

[패키지 구성 — 책임별로 분리]
  config.py    : 환경변수/상수(전부 여기서만 읽음)
  urlutil.py   : host_of() — 순환 임포트 방지용 최소 URL 유틸
  llm.py       : LLM#2/#3 호출
  browser.py   : Playwright 페이지 조작/파싱 원시 함수(판단 없음)
  antibot.py   : "이 URL을 확정해도 되는가" 방어 규칙(네이버 블로그/판매종료/링크인바이오 중첩 등)
  redirect.py  : 판단 없는 리다이렉트 추적
  links.py     : 페이지/링크인바이오 허브에서 후보 링크 목록 뽑기
  ranking.py   : candidate_url 원본 후보들의 시도 순서 정하기 + 명백히 무관한 후보 제거
  youtube.py   : 유튜브 전용 링크 복구(잘린 캡션 URL, 채널 정보란 링크)
  matching.py  : 상품명/컨텍스트 텍스트 휴리스틱
  picker.py    : LLM#2로 링크 고르고 확신도별로 확정/재검증(finalize_pick)
  core.py      : 후보 순회 상태 기계(resolve_product) — 이 패키지의 오케스트레이션 본체
  runner.py    : 워커 풀 실행/체크포인트/CLI 진입점(main)

사용법(scripts/ 디렉터리에서):
    python3 -m resolve_links            # load_ready.json 전체(아직 해석 안 된 상품만)
    python3 -m resolve_links 50         # 상품 단위로 50건만
체크포인트: data/output/link_resolution.json (10건마다 저장 — Ctrl+C로 중단해도 다시 실행하면
           이어서 진행됨)
결과: data/output/load_ready_resolved.json
"""
from .core import resolve_product
from .matching import product_key

# runner(main)는 여기서 임포트하지 않는다 — runner가 공용 crawl_pool을 쓰고 crawl_pool이
# 이 패키지의 browser를 쓰기 때문에(2단계 B3), 패키지 로드 시점에 runner까지 끌어오면
# 순환 임포트가 된다. CLI 진입은 __main__.py가 runner를 직접 부른다.

__all__ = ['resolve_product', 'product_key']
