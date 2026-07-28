# 공구왕 포스트 분류 파이프라인

인스타그램/유튜브 원본 데이터(hifen DB)에서 "확실한 공구"만 최대한 보수적으로 걸러내
플랫폼별 테이블(dev_gongguking DB의 `gonggu_post`/`gonggu_post_product` — 인스타그램,
`gonggu_video`/`gonggu_video_product` — 유튜브)에 저장하는 파이프라인입니다.

**범위: 링크를 "하나로 확정"하는 것까지.** 그 확정된 링크를 실제로 열어서 가격/이미지/옵션
등 진짜 상품 데이터를 가져오는 것은 이 테이블을 읽어가는 별도 개발자의 담당이며, 이 저장소에는
포함되지 않습니다. 전체 그림은 `docs/pipeline_diagram.html`을 브라우저로 열어서 보세요(단,
링크 해석 단계 추가 전 버전이라 최신 아키텍처는 아래 다이어그램을 참고).

## 아키텍처 — 큰 그림

```
hifen DB (읽기 전용, 최근 N일 "공구"/"공동구매" 키워드 매칭 포스트)
   ↓ 1. fetch_source.py                                    data/01_raw/<발행일>.jsonl
LLM #1 — 공구 여부 판별 + 상품 배열(상품마다 link_location/url_type/urls) + 시작·종료일
   ↓ 2. classify.py                                        data/02_classified/<발행일>.jsonl
게이트(코드, 보수적) — is_gonggu=false / 상품 특정 실패 / 제휴 광고성 다중 링크 → 제외
   ↓ 3. transform.py                                       data/03_load_ready/<발행일>.jsonl
                                                   [candidate_url = LLM 원본 후보 목록]
크롤링(Playwright) → LLM#3(페이지판별) → 링크모음/스토어메인이면 LLM#2(링크선택)로
하나 고름 → 즉시 최종 확정(재검증 없음, 안티봇 차단 회피)
   ↓ 4. resolve_links (패키지)                             data/04_resolved/<발행일>.jsonl
                                                   [candidate_url = 해석된 최종 링크 1개]
dev_gongguking DB
  - 유튜브: gonggu_video(영상 1건) + gonggu_video_product(상품, 1:N)
  - 인스타그램: gonggu_post(포스트 1건) + gonggu_post_product(상품, 1:N)
   ↑ 5. load.py (이미 있는 video_id/post_id는 새로 안 넣고 스킵)

──── 여기서부터는 "이미 DB에 들어간 것"을 매일 다시 손보는 보강 단계 ────

   6. update_gonggu_stage.py  — gonggu_stage를 오늘 날짜 기준으로 갱신(시작전→진행중→종료)
   7. rescan_inprogress.py    — "진행중"인데 아직 링크를 못 찾은(unresolved) 상품만 재탐색
```

1~5가 "새 포스트를 처음부터 끝까지 처리"하는 본줄기, 6~7은 "이미 처리된 것 중 상태가 바뀐
것만 골라 다시 손보는" 보강 단계입니다. 아래 "모듈 하나씩 뜯어보기"에서 각 단계를 순서대로
자세히 설명합니다 — 처음 보신다면 그 순서대로 읽으시는 걸 추천합니다.

`resolve_links`는 실제 크롤링(안티봇 회피 대기 포함)이라 느립니다 — 안 돌렸거나 건너뛰면
`load.py`는 `transform.py`가 만든 원본 후보 목록(세미콜론으로 이어붙인 상태)을 그대로 씁니다.
`load.py`는 이미 DB에 있는 post_id/video_id를 건너뛰기만 하고 UPDATE는 하지 않으므로, 링크
해석은 반드시 load 전에 끝나 있어야 DB에 반영됩니다 — 그래서 1~5의 순서가 고정이고 나중에
따로 붙이는 방식은 못 씁니다.

`scripts/resolve_links/`와 `scripts/linkbio_parser/`는 파일 하나가 아니라 책임별로 나뉜
패키지입니다(각각 10개 안팎의 파일, 파일당 200줄 이하) — 구성은 각 패키지의
`__init__.py` 상단 docstring 참고. 그래서 `python3 scripts/resolve_links.py`가 아니라
**`cd scripts && python3 -m resolve_links`**로 실행합니다.

컬럼명/타입은 hifen DB의 대응 컬럼(`YT_video_lists.video_id`, `instagram_post.user_id` 등)과
최대한 동일하게 맞춰져 있습니다 — 실제로 조인하진 않지만 봤을 때 바로 알아볼 수 있도록. 자세한
근거는 `queries/create_gonggu_tables.sql` 상단 주석 참고. link_location/url_type/candidate_url은
포스트가 아니라 **상품(product) 테이블**에 있습니다 — 한 포스트에 상품이 여러 개면 상품마다
구매 링크 위치·종류가 다를 수 있기 때문입니다.

## 모듈 하나씩 뜯어보기

파이프라인을 처음 보신다면 이 순서대로 읽으시면 됩니다. 각 모듈은 "무엇을 하는지 / 무엇을
입력받아 무엇을 만드는지 / 실행 명령 / 알아두면 좋을 점"으로 정리했습니다.

### 1. `fetch_source.py` — 원본 수집

- **무엇**: hifen DB(원본 인스타/유튜브 데이터)에서 최근 N일치 중 캡션에 "공구"/"공동구매"가
  들어간 포스트·영상만 SQL로 걸러 가져옵니다. 아직 아무 판단도 안 한 원본 그대로입니다.
- **입력**: hifen DB (읽기 전용)
- **출력**: `data/01_raw/<발행일>.jsonl` — 발행일별로 쪼개서 저장. 이번에 가져온 기간(`DAYS_BACK`)에
  해당하는 날짜 파일만 새로 씁니다(그 밖의 날짜 파일은 그대로 둠).
- **명령**: `DAYS_BACK=7 python3 scripts/fetch_source.py`
- **알아둘 점**: `FETCH_FIRST`라는 환경변수는 이 스크립트가 아니라 `run_pipeline.py`에서만
  쓰입니다 — `fetch_source.py`를 직접 실행할 땐 무의미(에러는 안 나지만 아무 효과 없음).

### 2. `classify.py` — LLM#1 공구 분류

- **무엇**: 01_raw의 각 포스트를 LLM#1(DeepSeek, 프롬프트는 `scripts/prompts.py`의
  `GONGGU_CLASSIFY_SYSTEM`)에 태워서 "공구인지(is_gonggu)", "상품이 몇 개인지(products 배열,
  상품마다 link_location/url_type/urls)", "공구 시작·종료일"을 뽑아냅니다. 아직 필터링은 안
  하고 판단 결과만 붙입니다.
- **입력**: `data/01_raw/*.jsonl` 중 아직 분류 안 된 것
- **출력**: `data/02_classified/<발행일>.jsonl` — 원본 포스트 + `classification` 필드
- **명령**: `CONCURRENCY=24 python3 scripts/classify.py`
  - `LIMIT=500` — 이번 실행에 500건만(체크포인트는 이어서)
  - `PLATFORM=yt` — ig/yt 중 하나만
- **알아둘 점**:
  - **재시도 설계**: 실패한 건(`classification_error`)은 "완료"로 안 치고 다음 실행에서
    자동으로 다시 시도합니다. 429(레이트리밋)는 최대 10회, 최대 60초 대기까지 길게
    재시도합니다 — 코드 버그가 아니라 잠깐 기다리면 풀리는 상태이기 때문입니다.
  - **저장 방식**: 결과 1건 = 그 날짜 파일에 한 줄 append. 건수가 몇만 건이 되어도 저장
    비용이 늘지 않습니다(예전엔 날짜 파일 전체를 매번 다시 썼어서 건수가 쌓일수록 느려졌음).

### 3. `transform.py` — 보수적 게이트링

- **무엇**: classify.py가 붙인 `classification`을 보고 "확실한 공구만" 코드로 걸러냅니다.
  LLM 호출 없이 순수 규칙 기반이라 빠르고 결정론적입니다. 제외 사유: `is_gonggu=false`,
  상품을 하나도 특정 못함, 제휴 광고성 다중 링크(쿠팡파트너스 등).
- **입력**: `data/02_classified/*.jsonl` 전체
- **출력**: `data/03_load_ready/<발행일>.jsonl` — `{platform, parent: {...}, products: [...]}`
  형태로 DB 테이블 구조에 가깝게 정리됨. `candidate_url`은 아직 LLM이 뽑은 원본 후보 목록
  (세미콜론으로 이어붙인 상태)입니다.
- **명령**: `python3 scripts/transform.py` (제외 사유별 건수까지 같이 출력)
- **알아둘 점**: 매번 02_classified **전체**를 처음부터 다시 계산합니다(누적하지 않음) — 그래서
  실행할 때마다 기존 03_load_ready 날짜 파일을 지우고 새로 씁니다. 필터링 규칙을 바꾸면
  이 스크립트만 다시 돌리면 전체가 그 새 규칙대로 재계산됩니다.

### 4. `resolve_links` (패키지) — 링크 해석

- **무엇**: `candidate_url`의 후보 링크들을 실제로 열어봐서(Playwright) "진짜 구매 가능한
  최종 링크 1개"로 확정합니다. 인스타 공구는 프로필 링크(대부분 인포크/링크트리 같은
  "링크인바이오" 허브)를 거치는 경우가 많아서, 그런 페이지면 브라우저 없이 구조화 데이터로
  빠르게 후보를 뽑고(LLM#2로 그중 하나 선택), 아니면 브라우저로 열어서 LLM#3로 상품페이지인지
  판별합니다. LLM#2/#3도 LLM#1과 같은 DeepSeek 호출(`scripts/resolve_links/llm.py`)입니다.
- **입력**: `data/03_load_ready/*.jsonl` 중 아직 해석 안 된 상품
- **출력**: `data/04_resolved/<발행일>.jsonl` (최종 후보 반영) + `data/output/link_resolution.jsonl`
  (상품 단위 체크포인트 — 상품 key당 결과 1줄, 재실행 시 이미 처리된 건 건너뜀)
- **명령**(반드시 `scripts/` 디렉터리에서 `-m`으로 실행):
  ```
  cd scripts && RESOLVE_CONCURRENCY=30 python3 -m resolve_links
  cd scripts && python3 -m resolve_links 50   # 50건만 끊어서 테스트
  ```
- **알아둘 점**:
  - **워커 1개 = 브라우저 1개**라 `RESOLVE_CONCURRENCY`를 올리면 메모리를 많이 씁니다.
    맥북(10코어/16GB급) 기준 30까지는 실측으로 안전했지만, 다른 무거운 앱(크롬/VSCode 등)이
    같이 떠 있으면 여유 메모리가 부족해질 수 있으니 `uptime`/활성상태 보기로 살펴가며 조절할 것.
  - **`MAX_PER_DOMAIN`**(기본 4): 같은 목적지 도메인(스마트스토어 등)에 동시에 몰리는 걸
    막는 상한 — `browser.py`의 `fetch()`/`redirect.py`의 `follow_redirect()`가 실제로
    페이지를 여는 그 순간에 도메인 기준으로 게이팅합니다(후보 링크의 "첫 번째" 도메인이 아니라
    "실제로 여는" 도메인 기준이라, 인포크처럼 가벼운 1차 홉은 이 제한에 안 걸리고 무거운
    2차 홉(실제 쇼핑몰)만 제대로 보호됨).
  - **인포크 등 링크인바이오 캐시**: 같은 인플루언서 계정을 형제 상품 여러 개가 공유하는
    경우가 많아서(실측: 평균 2.7배 중복), 같은 URL은 프로세스 안에서 한 번만 실제로 요청하고
    재사용합니다.
  - **`python3 scripts/login_naver.py`**: 네이버에 직접 로그인해서 세션을
    `data/auth/session_state.json`에 저장해두면, 이후 모든 resolve_links 워커가 로그인된
    상태로 스마트스토어/블로그에 접근합니다(로그인월로 튕기는 페이지를 실제 계정으로 우회 —
    안티봇을 속이는 게 아니라 진짜 로그인이라 더 안전함). 이 파일엔 실제 로그인 쿠키가 들어있으니
    (`.gitignore`로 커밋은 막아둠) 복사/공유하지 말 것.
  - **동시에 두 번 실행하지 말 것**: `MAX_PER_DOMAIN`은 프로세스 하나 안에서만 관리되는
    값이라, 두 인스턴스를 동시에 돌리면 같은 도메인에 실제로는 두 배까지 몰릴 수 있습니다.

### 5. `load.py` — DB 적재

- **무엇**: `04_resolved`(해석까지 끝났으면) 또는 `03_load_ready`(안 돌렸으면, 원본 후보
  그대로)를 dev_gongguking에 INSERT합니다.
- **입력**: `data/04_resolved/*.jsonl`가 있으면 그걸, 없으면 `data/03_load_ready/*.jsonl`
- **출력**: `gonggu_post`/`gonggu_post_product`(인스타) 또는 `gonggu_video`/`gonggu_video_product`(유튜브)
- **명령**: `python3 scripts/load.py`
- **알아둘 점**:
  - **이미 있는 post_id/video_id는 완전히 스킵**합니다(UPDATE 없음) — 그래서 링크 해석이
    끝난 뒤에 실행해야 하고, 나중에 다시 실행해도 새로 생긴 것만 추가됩니다.
  - 건별로 커밋해서 하나 실패해도(예: 값이 컬럼 길이 초과) 나머지는 정상 삽입됩니다.
  - **동시에 두 번 실행하면 안 됩니다** — 두 프로세스가 "존재 확인 → 없으면 INSERT"를 거의
    동시에 하면 그 사이에 서로 끼어들어 `Duplicate entry` 에러가 납니다(데이터가 깨지는 건
    아니고 — 둘 중 하나가 먼저 넣은 걸 다른 하나가 다시 넣으려다 막힌 것뿐 — 하지만 헷갈리니
    피할 것).

### 6. `update_gonggu_stage.py` — 공구 상태 갱신 (매일 보강)

- **무엇**: `gonggu_start_date`/`gonggu_end_date`를 **오늘 날짜**와 비교해서 `gonggu_stage`
  (`시작전`/`진행중`/`종료`/`판단불가`)를 갱신합니다. LLM 재호출 없이 순수 날짜 비교 +
  UPDATE만 하는 정적 배치라 빠릅니다.
- **입력/출력**: `gonggu_post`/`gonggu_video` 테이블 자체(파일 관여 없음)
- **명령**: `python3 scripts/update_gonggu_stage.py`
- **알아둘 점**: 이미 `종료`인 행은 다시 열릴 일이 없으므로 조회 대상에서 아예 제외합니다 —
  그래서 실제로 확인하는 전이는 `시작전 → 진행중/종료`, `진행중 → 종료` 두 가지뿐입니다.
  매일 실행해도 이미 맞게 계산된 행은 그대로 두므로(idempotent) 하루에 여러 번 돌려도
  안전합니다. transform.py의 날짜 비교 로직(`_compute_stage`)을 그대로 재사용해서 적재
  시점 계산과 어긋나지 않습니다.

### 7. `rescan_inprogress.py` — 진행중 공구 링크 재탐색 (매일 보강)

- **무엇**: 공구가 "시작전"일 때는 인포크 등에 아직 실제 구매 링크가 안 걸려 있어서
  `link_status='unresolved'`로 남는 경우가 많은데, "진행중"이 되면 그 링크가 채워지는
  경우가 많습니다. 그래서 **지금 `gonggu_stage='진행중'`인데 아직 `unresolved`인 상품만**
  골라서 4번(resolve_links)과 똑같은 판단/크롤링 로직(`resolve_product`)으로 다시
  시도합니다.
- **입력**: DB에서 직접 조회(파일 안 거침 — `unresolved` 상품의 `candidate_url`엔 LLM이
  뽑은 원본 후보가 그대로 보존되어 있어서 재시도에 필요한 정보가 DB에 다 있음)
- **출력**: `done`으로 바뀐 상품만 해당 행의 `candidate_url`/`link_status`를 UPDATE.
  여전히 `unresolved`면 그대로 둡니다. 동시에 `link_resolution.jsonl`에도 같은 키로
  결과를 append해서, 나중에 04_resolved를 다시 조립해도 이 재탐색 결과가 안 잊혀지게 합니다.
- **명령**: `RESCAN_CONCURRENCY=6 python3 scripts/rescan_inprogress.py` (`LIMIT=50`으로
  소규모 테스트 가능)
- **알아둘 점**: **6번(`update_gonggu_stage.py`) 다음에 실행해야 합니다** — 오늘자
  "진행중" 상태가 먼저 확정돼 있어야 그걸 기준으로 대상을 고를 수 있습니다. resolve_links와
  마찬가지로 동시에 두 개(또는 resolve_links와 동시에) 돌리지 말 것.

## 설치

```bash
pip install -r requirements.txt
playwright install chromium   # resolve_links(링크 해석 단계)용 — 최초 1회만
cp .env.example .env          # 값 채우기 (DB 자격증명, DEEPSEEK_KEY)
```

## LLM 설정

LLM#1~#4(공구판별/링크선택/페이지판별/카테고리분류) 전부 DeepSeek API를 직접 호출합니다 —
Dify 같은 외부 워크플로우 도구에 의존하지 않고, 프롬프트와 호출 로직이 이 저장소 코드
(`scripts/common.py`의 `call_llm`, `scripts/prompts.py`의 시스템 프롬프트 4개) 안에 그대로
있습니다. `.env`에 `DEEPSEEK_KEY`만 채우면 됩니다(`DEEPSEEK_MODEL` 기본값은 `deepseek-v4-pro`).

## DB 스키마

`queries/create_gonggu_tables.sql` — dev_gongguking에 적용할 DDL(4개 테이블: gonggu_video,
gonggu_video_product, gonggu_post, gonggu_post_product). 신규 설치용이며 DROP을 포함하지
않는다 — 테이블이 이미 있으면 그냥 에러로 멈출 뿐 기존 데이터는 건드리지 않는다(안전한
실패). 기존 데이터를 밀고 처음부터 다시 만들어야 할 때만 위험을 인지한 상태로
`queries/reset_gonggu_tables.sql`(DROP 문 + 백업 경고)을 먼저 실행할 것.

## 사용법

### 매일 돌리는 순서 (권장)

위 "모듈 하나씩 뜯어보기" 1~7번을 이 순서 그대로, 하루에 한 번 실행하면 됩니다. 6번이
5번보다 먼저 와야 그날 "진행중" 상태가 먼저 확정되고, 7번이 그걸 기준으로 재탐색 대상을
고를 수 있습니다.

```bash
python3 scripts/update_gonggu_stage.py                       # 6. 공구 상태(시작전/진행중/종료) 갱신
DAYS_BACK=7 python3 scripts/fetch_source.py                  # 1. 원본 수집
CONCURRENCY=24 python3 scripts/classify.py                   # 2. LLM#1 공구 분류
python3 scripts/transform.py                                 # 3. 보수적 게이트링
cd scripts && RESOLVE_CONCURRENCY=30 python3 -m resolve_links # 4. 링크 해석 (scripts/ 안에서 -m으로!)
cd .. && python3 scripts/load.py                              # 5. DB 적재
python3 scripts/rescan_inprogress.py                          # 7. 진행중인데 못 찾은 링크 재탐색
```

**한 단계가 끝나야 다음 단계로 넘어갑니다** — 각 스크립트는 그 시점 데이터 전체를 처리하고
끝나므로, 순서대로 하나씩 실행하세요. `CONCURRENCY`/`RESOLVE_CONCURRENCY` 숫자는 본인 컴퓨터
사양에 맞게 조절할 것(너무 높으면 메모리 부족으로 시스템이 느려질 수 있음 — "모듈 하나씩
뜯어보기"의 4번 항목 참고). **같은 단계를 두 터미널에서 동시에 실행하지 마세요** — classify.py/
resolve_links/load.py 모두 동시 실행 시 문제가 생긴 전례가 있습니다.

### 한 번에 자동으로 (1~5번만)

`run_pipeline.py`가 1~5번을 정해진 순서로 이어 부르는 오케스트레이터입니다. **6·7번(공구
상태 갱신, 진행중 재탐색)은 아직 포함되어 있지 않으므로 따로 실행해야 합니다.**

```bash
python3 scripts/run_pipeline.py                              # 이미 fetch했다는 전제, 1~5단계 순서대로
FETCH_FIRST=1 python3 scripts/run_pipeline.py                 # 원본부터 새로 가져오는 것부터 시작
FETCH_FIRST=1 DAYS_BACK=14 python3 scripts/run_pipeline.py    # 최근 14일치로 새로 가져오기
python3 scripts/run_pipeline.py --skip-resolve                # 링크 해석 건너뛰고 원본 후보로 바로 load
python3 scripts/run_pipeline.py --skip-load                   # DB에 안 넣고 03_load_ready까지만 확인
```

끝나면 `dev_gongguking`의 4개 테이블 현재 행 수를 보여줍니다.

### 파일 형식 참고

모든 중간 산출물은 폴더 이름(`01_raw` ~ `04_resolved`)이 곧 "어느 단계에서 나온 결과인지"를
보여주고, 그 안에서 다시 발행일(`YYYY-MM-DD.jsonl`) 파일로 쪼개져 있어서 특정 날짜 포스트가
지금 어느 단계까지 처리됐는지 파일 하나만 열어도 바로 보입니다. **JSONL(레코드 1개=1줄)** 형식이라
grep/head로 한 줄씩 바로 들여다볼 수 있고, classify.py처럼 계속 이어서 실행되는 단계는 결과가
나올 때마다 파일 끝에 한 줄만 추가(append)하므로 건수가 아무리 쌓여도 저장 비용이 늘지 않습니다
(예전엔 배열 하나를 통째로 다시 써서 건수가 많아질수록 저장이 느려졌음 — 2026-07-27 실측/수정).
날짜를 못 읽은 레코드는 `_unknown.jsonl`에 모입니다. `link_resolution.jsonl`(resolve_links
내부의 상품 단위 체크포인트)만 날짜 필드가 없는 key-value 저장이라 예외적으로 단일 파일이지만,
같은 이유로 JSONL append 방식입니다.

### 보조/진단 스크립트

- `scripts/check_db.py` — 소스/타겟 DB 연결과 타겟 테이블 스키마를 확인하는 점검 스크립트.
- `scripts/_diag_sample.py` — 링크 해석 품질을 점검하고 싶을 때 쓰는 진단용 스크립트. 실제
  파이프라인 체크포인트(02_classified/03_load_ready 등)는 건드리지 않고, `01_raw`에서 랜덤
  샘플을 뽑아 classify→transform→resolve_links를 돌려서 `data/output/_diag_result.json`에
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
