# 공구왕 포스트 분류 파이프라인

인스타그램/유튜브 원본 데이터(hifen DB)에서 "확실한 공구"만 최대한 보수적으로 걸러내
플랫폼별 테이블(dev_gongguking DB의 `gonggu_post`/`gonggu_post_product` — 인스타그램,
`gonggu_video`/`gonggu_video_product` — 유튜브)에 저장하는 파이프라인입니다.

**범위: 링크를 "하나로 확정"하는 것까지.** 그 확정된 링크를 실제로 열어서 가격/이미지/옵션
등 진짜 상품 데이터를 가져오는 것은 이 테이블을 읽어가는 별도 개발자의 담당이며, 이 저장소에는
포함되지 않습니다. 전체 그림은 `docs/pipeline_diagram.html`을 브라우저로 열어서 보세요(단,
링크 해석 단계 추가 전 버전이라 최신 아키텍처는 아래 다이어그램을 참고).

## 아키텍처

```
hifen DB (읽기 전용, 최근 N일 "공구"/"공동구매" 키워드 매칭 포스트)
   ↓ fetch_source.py
LLM #1 — 공구 여부 판별 + 상품 배열(상품마다 link_location/url_type/urls) + 시작·종료일
   ↓ classify.py
게이트(코드, 보수적) — is_gonggu=false / 상품 특정 실패 / 제휴 광고성 다중 링크 → 제외
   ↓ transform.py                                          [candidate_url = LLM 원본 후보 목록]
크롤링(Playwright, 1회) → LLM#3(페이지판별) → 링크모음/스토어메인이면 LLM#2(링크선택)로
하나 고름 → 그 링크를 실제로 열어서 재검증하지 않고 즉시 최종 확정(안티봇 차단 회피,
마감/예정 등 진행 단계와 무관하게 항상 시도)      [candidate_url = 해석된 최종 링크 1개]
   ↓ resolve_links (패키지)
dev_gongguking DB
  - 유튜브: gonggu_video(영상 1건) + gonggu_video_product(상품, 1:N)
  - 인스타그램: gonggu_post(포스트 1건) + gonggu_post_product(상품, 1:N)
   ↑ load.py (이미 있는 video_id/post_id는 재삽입 안 함)
```

`resolve_links`는 실제 크롤링(안티봇 회피 대기 포함)이라 느립니다 — 안 돌렸거나 건너뛰면
`load.py`는 `transform.py`가 만든 원본 후보 목록(세미콜론으로 이어붙인 상태)을 그대로 씁니다.
`load.py`는 이미 DB에 있는 post_id/video_id를 건너뛰기만 하고 UPDATE는 하지 않으므로, 링크
해석은 반드시 load 전에 끝나 있어야 DB에 반영됩니다 — 그래서 이 순서가 고정이고 나중에 따로
붙이는 방식은 못 씁니다.

**`run_pipeline.py`가 이 5단계를 정해진 순서로 이어 부르는 오케스트레이터입니다.** 스테이지
하나가 그 시점의 데이터 전체를 완전히 처리하고 나서야 다음 스테이지로 넘어갑니다(청크
단위로 여러 스테이지를 번갈아 도는 방식이 아님) — 동시성/체크포인트는 각 스테이지 스크립트
자체가 갖고 있으므로 오케스트레이터는 순서 보장에만 집중합니다. 자세한 사용법은 아래
"사용법" 절 참고.

`scripts/resolve_links/`와 `scripts/linkbio_parser/`는 파일 하나가 아니라 책임별로 나뉜
패키지입니다(각각 10개 안팎의 파일, 파일당 200줄 이하) — 구성은 각 패키지의
`__init__.py` 상단 docstring 참고.

컬럼명/타입은 hifen DB의 대응 컬럼(`YT_video_lists.video_id`, `instagram_post.user_id` 등)과
최대한 동일하게 맞춰져 있습니다 — 실제로 조인하진 않지만 봤을 때 바로 알아볼 수 있도록. 자세한
근거는 `queries/create_gonggu_tables.sql` 상단 주석 참고. link_location/url_type/candidate_url은
포스트가 아니라 **상품(product) 테이블**에 있습니다 — 한 포스트에 상품이 여러 개면 상품마다
구매 링크 위치·종류가 다를 수 있기 때문입니다.

## 설치

```bash
pip install -r requirements.txt
playwright install chromium   # resolve_links(링크 해석 단계)용 — 최초 1회만
cp .env.example .env          # 값 채우기 (DB 자격증명, Dify API 키 3개)
```

## Dify 워크플로우 준비

앱 3개를 각각 Dify에서 "DSL로 가져오기"로 **새 앱**으로 import하고, 발급된 API 키를 `.env`에
채웁니다. 기존 앱을 덮어쓰지 말 것 — 이 프로젝트 전용으로 새로 만들 것.

- `dify_workflows/01_gonggu_classify.yml` (공구 판별 + 상품 추출) → `.env`의 `DIFY_KEY`.
  모델은 원래 gpt-5-mini 기준으로 짰지만 현재는 소넷5로 교체해서 씀.
- `dify_workflows/02_link_selection.yml` ("공구왕 링크선택" — 링크모음 페이지의 후보 중 이
  포스트 상품에 맞는 것 하나 고르기) → `.env`의 `DIFY_KEY_PICK`.
- `dify_workflows/03_page_judge.yml` ("공구왕 페이지판별" — 도착한 페이지가 최종 상품페이지인지
  판별) → `.env`의 `DIFY_KEY_JUDGE`.

02/03은 `7월_co_buying_data/gonggu-link-resolver` 프로젝트에서 만든 걸 그대로 가져온
것입니다(입력이 `post_context`/`candidates`/`page` 같은 범용 구조라 이 프로젝트의
`products` 배열 스키마와 무관하게 재사용 가능) — 모델은 gpt-5-mini 그대로 둬도 되고,
비용/품질 보고 나중에 바꿔도 됩니다.

## DB 스키마

`queries/create_gonggu_tables.sql` — dev_gongguking에 적용할 DDL(4개 테이블: gonggu_video,
gonggu_video_product, gonggu_post, gonggu_post_product). 신규 설치용이며 DROP을 포함하지
않는다 — 테이블이 이미 있으면 그냥 에러로 멈출 뿐 기존 데이터는 건드리지 않는다(안전한
실패). 기존 데이터를 밀고 처음부터 다시 만들어야 할 때만 위험을 인지한 상태로
`queries/reset_gonggu_tables.sql`(DROP 문 + 백업 경고)을 먼저 실행할 것.

## 사용법

파이프라인은 5개 스테이지 스크립트로 나뉘어 있고(각자 체크포인트를 가지므로 Ctrl+C로
언제든 중단해도 다시 실행하면 이어서 진행됨), `run_pipeline.py`가 이 스테이지들을 정해진
순서로 — 한 스테이지가 그 시점 데이터 **전체**를 끝낸 뒤에야 다음 스테이지로 — 이어 부릅니다.

| 스테이지 | 스크립트 | 무엇을 전체 처리하나 |
|---|---|---|
| 1. 원본 수집 | `fetch_source.py` | hifen DB → `data/raw/posts_raw.json` |
| 2. 공구 분류 | `classify.py` | posts_raw.json 중 미분류 전체 → `data/output/classified.json` |
| 3. 게이트링 | `transform.py` | classified.json 전체(항상 재계산) → `data/output/load_ready.json` |
| 4. 링크 해석 | `resolve_links`(패키지) | load_ready.json 중 미해석 상품 전체 → `data/output/load_ready_resolved.json` |
| 5. DB 적재 | `load.py` | 위 결과 전체 중 미삽입 건 → `dev_gongguking` |

**사람 개입 없이 끝까지 자동으로 돌리고 싶을 때:**

```bash
python3 scripts/run_pipeline.py                              # 이미 fetch했다는 전제, 5단계 순서대로
FETCH_FIRST=1 python3 scripts/run_pipeline.py                 # 원본부터 새로 가져오는 것부터 시작
FETCH_FIRST=1 DAYS_BACK=14 python3 scripts/run_pipeline.py    # 최근 14일치로 새로 가져오기
python3 scripts/run_pipeline.py --skip-resolve                # 링크 해석 건너뛰고 원본 후보로 바로 load
python3 scripts/run_pipeline.py --skip-load                   # DB에 안 넣고 load_ready.json까지만 확인
```

끝나면 `dev_gongguking`의 4개 테이블 현재 행 수를 보여줍니다.

**모듈별로 따로 실행**(중간 결과를 직접 확인하며 진행하고 싶을 때 — 각 명령은 그 스테이지의
남은 데이터 전체를 한 번에 처리합니다):

```bash
python3 scripts/fetch_source.py                       # data/raw/posts_raw.json
python3 scripts/classify.py                           # data/output/classified.json (10건마다 체크포인트, 이어서 실행 가능)
PLATFORM=yt LIMIT=500 python3 scripts/classify.py     # ig/yt 중 하나만, N건만 끊어서(수동 테스트용)
python3 scripts/transform.py                          # data/output/load_ready.json + 제외 사유별 건수 출력
cd scripts && python3 -m resolve_links                # data/output/load_ready_resolved.json (상품 10건마다 체크포인트, 이어서 실행 가능)
cd scripts && python3 -m resolve_links 50              # 상품 50건만 끊어서 테스트(수동 테스트용)
python3 scripts/load.py                               # dev_gongguking에 실제 INSERT (resolved 파일이 있으면 그걸 우선 사용, 건별 커밋)
```

`resolve_links`는 파일 하나가 아니라 패키지(`scripts/resolve_links/`)라 `python3 scripts/resolve_links.py`가
아니라 `python3 -m resolve_links`로 실행합니다(scripts/ 디렉터리에서). `RESOLVE_CONCURRENCY`(기본 1)/
`ITEM_DELAY`(기본 3초)로 워커 수·대기시간을 조절할 수 있지만, 올리기 전에 `_diag_sample.py`로
차단율을 먼저 확인할 것 — 네이버 등 최종 목적지 사이트에 동시 요청이 몰리면 안티봇에 걸리기 쉽습니다.

`scripts/check_db.py` — 소스/타겟 DB 연결과 타겟 테이블 스키마를 확인하는 점검 스크립트.

`scripts/_diag_sample.py` — 링크 해석 품질을 점검하고 싶을 때 쓰는 진단용 스크립트. 실제
파이프라인 체크포인트(classified.json/load_ready.json 등)는 건드리지 않고, `posts_raw.json`에서
랜덤 샘플을 뽑아 classify→transform→resolve_links를 돌려서 `data/output/_diag_result.json`에
남긴다(포스트 원문·프로필 소개글·LLM들의 판단 근거까지 다 같이 저장되어 있어서 결과를 사람이
직접 하나씩 읽고 판단하기 좋음).

```bash
python3 scripts/_diag_sample.py            # 포스트 300개 랜덤 -> 후보 있는 상품 50개 랜덤
python3 scripts/_diag_sample.py 500 80     # 포스트 500개, 상품 80개
```

## 보수적 필터링 기준

- **is_gonggu**: "공구"라는 글자가 있어도 도구(전동공구 등) 리뷰거나, 그룹구매 특유의 신호
  (공구가/공구오픈/한정특가 등) 없이 개인 리뷰+일반 구매링크만 있으면 false.
- **products**: 원칙적으로 한 포스트=한 공구로 최대한 합쳐서 상품 1개로 판단하고, 정말 서로
  무관한 공구가 병렬로 나열된 경우에만 상품별로 쪼갠다(그 경우에만 상품마다 link_location/
  url_type/urls가 달라짐). 상품을 하나도 특정 못하면 통째로 제외(빈 배열 금지 원칙 — LLM이
  못 정하면 is_gonggu 자체를 재검토하도록 프롬프트에 명시).
- **날짜(gonggu_start_date/end_date)**: 캡션에 명시적 날짜가 있거나 게시일 기준 상대표현이
  명확할 때만 채움. 추측/환각 금지 — 애매하면 NULL(그래도 공구 자체가 확실하면 행은 저장됨).
- **제휴 광고성**: 쿠팡파트너스/네이버쇼핑커넥트 문구 + 링크 3개 이상이면 제외(TOP N 리뷰).
