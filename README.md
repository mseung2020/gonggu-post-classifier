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

**8번(`fetch_yt_ppl.py` + `classify_yt_ppl.py`)은 이 본줄기와 별도인 독립 유입 경로입니다**
— hifen DB의 `brand` 테이블(유튜브 PPL/브랜드 협찬 영상)에서 자체 SQL 쿼리로 가져와
(`fetch_yt_ppl.py`) 자체 프롬프트로 "공구" 여부를 판별한 뒤(`classify_yt_ppl.py`), 1~2번과
마찬가지로 `data/02_classified/`에 결과를 얹어서 3번(transform.py)부터는 본줄기와
합류합니다. LLM#1(`classify.py`)이나 위 1~7번 파일은 전혀 건드리지 않습니다.

`resolve_links`는 실제 크롤링(안티봇 회피 대기 포함)이라 느립니다 — 안 돌렸거나 건너뛰면
`load.py`는 `transform.py`가 만든 원본 후보 목록(세미콜론으로 이어붙인 상태)을 그대로 씁니다.
`load.py`는 이미 DB에 있는 post_id/video_id를 건너뛰기만 하고 UPDATE는 하지 않으므로, 링크
해석은 반드시 load 전에 끝나 있어야 DB에 반영됩니다 — 그래서 1~5의 순서가 고정이고 나중에
따로 붙이는 방식은 못 씁니다.

`gonggu/resolve_links/`와 `gonggu/linkbio_parser/`는 파일 하나가 아니라 책임별로 나뉜
하위 패키지입니다(각각 10개 안팎의 파일, 파일당 200줄 이하) — 구성은 각 패키지의
`__init__.py` 상단 docstring 참고.

**실행 규약(2026-08-05 패키지화 이후)**: 모든 모듈은 저장소 루트에서
`python3 -m gonggu.<모듈>`로 실행합니다(예: `python3 -m gonggu.classify`,
`python3 -m gonggu.resolve_links`). 예전의 `python3 scripts/x.py` /
`cd scripts && python3 -m resolve_links` 방식은 더 이상 쓰지 않습니다. `pip install -e .`를
한 번 해두면 어느 디렉터리에서든 `gonggu-classify` 같은 짧은 명령으로도 실행할 수
있습니다(pyproject.toml 참고). 코드를 고친 뒤에는 `python3 -m pytest`로 골든 diff를 확인합니다.

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
- **명령**: `DAYS_BACK=7 python3 -m gonggu.fetch_source`
- **알아둘 점**: `FETCH_FIRST`라는 환경변수는 이 스크립트가 아니라 `run_pipeline.py`에서만
  쓰입니다 — `fetch_source.py`를 직접 실행할 땐 무의미(에러는 안 나지만 아무 효과 없음).

### 2. `classify.py` — LLM#1 공구 분류

- **무엇**: 01_raw의 각 포스트를 LLM#1(DeepSeek, 프롬프트는 `gonggu/prompts.py`의
  `GONGGU_CLASSIFY_SYSTEM`)에 태워서 "공구인지(is_gonggu)", "상품이 몇 개인지(products 배열,
  상품마다 link_location/url_type/urls)", "공구 시작·종료일"을 뽑아냅니다. 아직 필터링은 안
  하고 판단 결과만 붙입니다.
- **입력**: `data/01_raw/*.jsonl` 중 아직 분류 안 된 것
- **출력**: `data/02_classified/<발행일>.jsonl` — 원본 포스트 + `classification` 필드
- **명령**: `CONCURRENCY=24 python3 -m gonggu.classify`
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
- **명령**: `python3 -m gonggu.transform` (제외 사유별 건수까지 같이 출력)
- **알아둘 점**: 기본은 **증분 모드**(2026-08-05, 대공사 3단계) — 지난 실행 이후 내용이 바뀐
  02_classified 날짜 파일만 다시 계산해 그 날짜의 03 파일만 교체합니다(변경 감지는 파일
  mtime+크기, `data/output/transform_state.json`). 이미 적재된 옛 날짜를 매번 다시 계산하던
  낭비가 사라졌습니다. **필터링 규칙을 바꿨을 때는 반드시 `--full`로 한 번** 돌리세요 —
  예전처럼 02 전체를 재계산하고 03을 전부 새로 씁니다.

### 4. `resolve_links` (패키지) — 링크 해석

- **무엇**: `candidate_url`의 후보 링크들을 실제로 열어봐서(Playwright) "진짜 구매 가능한
  최종 링크 1개"로 확정합니다. 인스타 공구는 프로필 링크(대부분 인포크/링크트리 같은
  "링크인바이오" 허브)를 거치는 경우가 많아서, 그런 페이지면 브라우저 없이 구조화 데이터로
  빠르게 후보를 뽑고(LLM#2로 그중 하나 선택), 아니면 브라우저로 열어서 LLM#3로 상품페이지인지
  판별합니다. LLM#2/#3도 LLM#1과 같은 DeepSeek 호출(`gonggu/resolve_links/llm.py`)입니다.
- **입력**: `data/03_load_ready/*.jsonl` 중 아직 해석 안 된 상품
- **출력**: `data/04_resolved/<발행일>.jsonl` (최종 후보 반영) + `data/output/link_resolution.jsonl`
  (상품 단위 체크포인트 — 상품 key당 결과 1줄, 재실행 시 이미 처리된 건 건너뜀)
- **명령**(저장소 루트에서):
  ```
  RESOLVE_CONCURRENCY=30 python3 -m gonggu.resolve_links
  python3 -m gonggu.resolve_links 50   # 50건만 끊어서 테스트
  ```
- **알아둘 점**:
  - **대기 최적화(2026-08-05, 4단계)**: 상품 사이 3초 대기(ITEM_DELAY)는 이제 "이번 상품에서
    실제로 브라우저를 쓴 경우"에만 적용됩니다(requests 패스트패스/캐시로 끝난 건은 안 쉼 —
    네이버 계열은 항상 브라우저 경로라 항상 대기 유지). 차단율이 이상해지면 `ITEM_DELAY_SMART=0`
    으로 끄면 예전처럼 매 상품 대기로 돌아갑니다. LLM 꼬리 지연이 심하면
    `LINK_LLM_TIMEOUT=45 LINK_LLM_TIMEOUT_RETRY=1`(옵트인 — 재시도라 답이 달라질 수 있음)도
    선택지입니다(gonggu/resolve_links/config.py 주석 참고).
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
  - **`python3 -m gonggu.login_naver`**: 네이버에 직접 로그인해서 세션을
    `data/auth/session_state.json`에 저장해두면, 이후 모든 resolve_links 워커가 로그인된
    상태로 스마트스토어/블로그에 접근합니다(로그인월로 튕기는 페이지를 실제 계정으로 우회 —
    안티봇을 속이는 게 아니라 진짜 로그인이라 더 안전함). 이 파일엔 실제 로그인 쿠키가 들어있으니
    (`.gitignore`로 커밋은 막아둠) 복사/공유하지 말 것.
  - **동시에 두 번 실행하지 말 것**: `MAX_PER_DOMAIN`은 프로세스 하나 안에서만 관리되는
    값이라, 두 인스턴스를 동시에 돌리면 같은 도메인에 실제로는 두 배까지 몰릴 수 있습니다.
  - **HTTP 패스트패스(`httpfetch.py`)**: LLM#3 판별에 실제로 쓰는 정보(title/og:image
    유무/JSON-LD/본문 텍스트 2000자)는 브라우저 없이 `requests`+`bs4`로도 대부분 얻을 수
    있습니다(건당 0.1~0.3초 vs Playwright 3~4초). 그래서 매 링크마다 먼저 이 패스트패스로
    시도하고, 정보가 부족하거나 차단된 낌새(429 등)가 있으면 그때만 기존 Playwright 경로로
    넘어갑니다(`BROWSER_ONLY_HOSTS`에 등록된 호스트는 애초에 패스트패스를 건너뜀). 실행이
    끝나면 패스트패스 적중률과 폴백 사유별 건수가 출력되므로, 느려졌다면 그 로그로 원인
    도메인을 바로 알 수 있습니다.

### 5. `load.py` — DB 적재

- **무엇**: `04_resolved`(해석까지 끝났으면) 또는 `03_load_ready`(안 돌렸으면, 원본 후보
  그대로)를 dev_gongguking에 INSERT합니다.
- **입력**: `data/04_resolved/*.jsonl`가 있으면 그걸, 없으면 `data/03_load_ready/*.jsonl`
- **출력**: `gonggu_post`/`gonggu_post_product`(인스타) 또는 `gonggu_video`/`gonggu_video_product`(유튜브)
- **명령**: `python3 -m gonggu.load`
- **알아둘 점**:
  - **소배치 커밋(2026-08-05, 4단계)**: 기본 50건(LOAD_BATCH)을 한 트랜잭션으로 넣고, 배치에서
    실패가 나면 그 배치만 건별 커밋으로 재처리합니다 — "한 건의 실패가 다른 건을 막지 않는다"는
    보장은 그대로, 커밋 왕복만 줄었습니다.
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
  UPDATE만 하는 정적 배치라 빠릅니다. 기간/스테이지가 상품 단위로 이전됨(2026-08-06)에 따라
  상품 행(`gonggu_post_product`/`gonggu_video_product`)을 PK(`id`) 기준으로 갱신합니다.
- **입력/출력**: `gonggu_post_product`/`gonggu_video_product` 테이블 자체(파일 관여 없음)
- **명령**: `python3 -m gonggu.update_gonggu_stage`
- **알아둘 점**: 이미 `종료`인 행은 다시 열릴 일이 없으므로 조회 대상에서 아예 제외합니다 —
  그래서 실제로 확인하는 전이는 `시작전 → 진행중/종료`, `진행중 → 종료` 두 가지뿐입니다.
  **강제 종료 규칙(2026-08-06 도입, 상품 이전 리팩터링에서 유실됐다가 2026-08-07 복원)**:
  시작일만 있고 종료일이 없는 공구는 날짜 비교만으로는 영원히 '진행중'으로 남으므로, 이
  케이스에 한해 시작일로부터 10일(`FORCE_END_AFTER_DAYS`, 0이면 끔)이 지나면 '종료'로 강제
  전환합니다. `gonggu_end_date`는 지어내지 않고 NULL 그대로 둡니다 — "종료인데 end_date가
  NULL" = 기간 미상으로 추정 종료된 행. 종료일이 명시된 공구는 며칠짜리든 절대 건드리지
  않습니다. 매일 실행해도 이미 맞게 계산된 행은 그대로 두므로(idempotent) 하루에 여러 번
  돌려도 안전합니다. transform.py의 날짜 비교 로직(`_compute_stage`)을 그대로 재사용해서
  적재 시점 계산과 어긋나지 않습니다.

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
- **명령**: `RESCAN_CONCURRENCY=6 python3 -m gonggu.rescan_inprogress` (`LIMIT=50`으로
  소규모 테스트 가능)
- **알아둘 점**: **6번(`update_gonggu_stage.py`) 다음에 실행해야 합니다** — 오늘자
  "진행중" 상태가 먼저 확정돼 있어야 그걸 기준으로 대상을 고를 수 있습니다. resolve_links와
  마찬가지로 동시에 두 개(또는 resolve_links와 동시에) 돌리지 말 것.

### 8. `fetch_yt_ppl.py` + `classify_yt_ppl.py` — 유튜브 PPL 공구 판별 (기존 파이프라인과 완전 독립)

- **무엇**: `fetch_source.py`는 캡션에 "공구"/"공동구매"가 그대로 적힌 유튜브 영상만
  키워드로 가져오는데, 실제로는 그 단어 없이 PPL/브랜드 협찬으로 진행되는 그룹특가(채널
  전용 할인코드, 브랜드x유튜버 콜라보 한정마켓 등)가 훨씬 많습니다. 이 두 스크립트는 hifen
  DB의 `brand` 테이블(브랜드 매칭된 유튜브 영상 전체)에서 "공구"/"공동구매" 키워드가 **없는**
  것만 모아, 전용 프롬프트(`prompts.YT_PPL_GONGGU_SYSTEM`)로 "PPL이지만 사실상 그룹특가인지"
  판별합니다. `fetch_source.py`/`classify.py`와 똑같이 **fetch 단계와 LLM 단계를 파일로
  분리**했습니다(`fetch_yt_ppl.py`는 LLM 호출 없이 raw만 저장, `classify_yt_ppl.py`가
  그 raw를 읽어 LLM 호출).
- **기존 LLM#1과 완전히 독립**: `classify.py`(LLM#1, `GONGGU_CLASSIFY_SYSTEM`)는 이 목적으로
  재사용하지 않습니다 — 이미 검증된 그 판정 로직/파일에 전혀 영향을 주지 않기 위해 fetch도,
  프롬프트도, 스크립트도 전부 별도로 분리했습니다. is_gonggu는 아래 세 경우만 true입니다:
  1. 브랜드x유튜버 콜라보 한정 마켓/기간한정 특가
  2. 채널/구독자 전용 할인코드·전용 할인율
  3. 기간이 명시된 "구독자 대상 특가"

  브랜드 상시할인 소개, 여행/구독 서비스 프로모코드, 신상소개 브이로그에 섞인 할인가, 게임
  인앱재화, 순수 브랜드노출, 개인리뷰+일반링크, 제휴마케팅(쿠팡파트너스 등), 댓글이벤트,
  후원/멤버십 유도는 전부 공구 아님으로 판단합니다.
- **`fetch_yt_ppl.py`**
  - **입력**: `brand` 테이블에서 `publishDate >= DAYS_BACK`이고 `video_description`에
    "공구"/"공동구매"가 없는 행 전체(SQL 단계에서부터 `fetch_source.py`가 다루는 video_id와
    상호 배타적이라 별도 dedup 불필요)
  - **출력**: `data/01_raw_yt_ppl/<발행일>.jsonl` — `fetch_source.py`의 `data/01_raw/`와는
    별도 디렉터리라 서로 덮어쓰지 않습니다.
  - **명령**: `DAYS_BACK=7 python3 -m gonggu.fetch_yt_ppl`
- **`classify_yt_ppl.py`**
  - **입력**: `data/01_raw_yt_ppl/*.jsonl` 중 아직 분류 안 된 것
  - **출력**: `data/02_classified/<발행일>.jsonl` — `classify.py`와 **같은 디렉터리**에 같은
    레코드 스키마로 append됩니다(서로 다른 video_id만 다루므로 충돌 없음). 그래서
    `transform.py`부터는 무수정으로 이 결과를 그대로 처리합니다.
  - **명령**: `CONCURRENCY=100 python3 -m gonggu.classify_yt_ppl` (`LIMIT=20`으로 소규모
    테스트 가능)
- **알아둘 점**: `classify.py`/`fetch_source.py`보다 먼저(또는 나중에) 돌려도 상관없습니다 —
  서로 완전히 독립이라 실행 순서가 결과에 영향을 주지 않습니다. 일일 퀘스트에서는
  `fetch_source.py` 다음에 `fetch_yt_ppl.py`, `classify.py` 다음에 `classify_yt_ppl.py`를
  추가하는 걸 권장합니다(두 LLM이 직렬로: `classify.py`가 끝나야 `classify_yt_ppl.py`가
  시작됨).

### 9. `backfill_period.py` — 공구기간 보강 크롤링 (매일 보강)

- **무엇**: 캡션에 명시적 날짜가 없어 상품 `gonggu_stage='판단불가'`로 남은 상품 중, 그 상품
  링크가 이미 `link_status='done'`으로 확정된 것을 골라 그 확정 상품페이지를 크롤링해서
  페이지 안에 그 상품의 공구기간이 적혀 있는지 LLM(`prompts.PERIOD_BACKFILL_SYSTEM`)으로
  찾습니다. 찾으면 그 상품의 `gonggu_start_date`/`gonggu_end_date`와 `gonggu_stage`를 그
  자리에서 같이 갱신합니다.
- **입력/출력**: `gonggu_post_product`/`gonggu_video_product` 테이블 자체(파일 관여 없음) +
  `data/output/period_backfill.jsonl`(체크포인트 — 찾았으면 영구 스킵, 못 찾았으면
  `PERIOD_RETRY_COOLDOWN_DAYS`일 쿨다운 후 재시도, `PERIOD_MAX_ATTEMPTS`회 넘으면 영구 스킵)
- **명령**: `python3 -m gonggu.backfill_period` (`LIMIT=20`으로 소규모 테스트,
  `BACKFILL_PERIOD_CONCURRENCY=4`로 동시성 조절)
- **알아둘 점**: 대상 자체가 상품 stage='판단불가'(그 상품의 시작일/종료일 둘 다 NULL)뿐이라
  이미 날짜가 있는 상품은 조회조차 안 돼 기존 값을 덮어쓸 여지가 구조적으로 없습니다.
  기간/스테이지가 상품 단위로 이전된 뒤(2026-08-06)로는 상품이 2개 이상인 게시물도
  스코프에 포함됩니다 — 예고 달력처럼 다중상품인 게시물의 각 상품도 자기 확정 페이지에서
  기간을 따로 찾습니다(예전엔 기간이 포스트 단위라 다중상품에서 어느 상품 기준인지
  모호해 단일상품만 대상으로 제한했는데, 그 제약이 사라졌습니다).
  `update_gonggu_stage.py`와 마찬가지로 `transform.py`의 `_compute_stage`를 재사용합니다.

## 설치

```bash
pip install -r requirements.txt
playwright install chromium   # resolve_links(링크 해석 단계)용 — 최초 1회만
cp .env.example .env          # 값 채우기 (DB 자격증명, DEEPSEEK_KEY)
pip install -e .              # (선택) gonggu-classify 같은 짧은 명령을 쓰려면
```

코드를 고친 뒤에는 저장소 루트에서 `python3 -m pytest`로 테스트를 돌려 골든 diff
(리팩터링 전후 판정 결과 동일성)가 깨지지 않았는지 확인합니다.

## LLM 설정

LLM#1~#4(공구판별/링크선택/페이지판별/카테고리분류) + 유튜브 PPL 공구 판별(8번,
`classify_yt_ppl.py` 전용)까지 전부 DeepSeek API를 직접 호출합니다 — Dify 같은 외부
워크플로우 도구에 의존하지 않고, 프롬프트와 호출 로직이 이 저장소 코드(`gonggu/common.py`의
`call_llm`, `gonggu/prompts.py`의 시스템 프롬프트들) 안에 그대로 있습니다. `.env`에
`DEEPSEEK_KEY`만 채우면 됩니다(`DEEPSEEK_MODEL` 기본값은 `deepseek-v4-pro`).
`YT_PPL_GONGGU_SYSTEM`은 `GONGGU_CLASSIFY_SYSTEM`(LLM#1)과 판단 기준이 완전히 다른
별도 프롬프트입니다 — 서로 절대 공유하지 않습니다.

## DB 스키마

`queries/create_gonggu_tables.sql` — dev_gongguking에 적용할 DDL(4개 테이블: gonggu_video,
gonggu_video_product, gonggu_post, gonggu_post_product). 신규 설치용이며 DROP을 포함하지
않는다 — 테이블이 이미 있으면 그냥 에러로 멈출 뿐 기존 데이터는 건드리지 않는다(안전한
실패). 기존 데이터를 밀고 처음부터 다시 만들어야 할 때만 위험을 인지한 상태로
`queries/reset_gonggu_tables.sql`(DROP 문 + 백업 경고)을 먼저 실행할 것.

## 사용법

### 매일 돌리는 순서 (권장)

**한 번에: `python3 -m gonggu.daily`** (2026-08-05 추가) — 아래 순서 전체를 한 명령으로
실행합니다. 각 단계의 stdout은 콘솔과 `data/logs/daily_<시각>.log`에 같이 남고, stderr
(Playwright 노이즈 등)는 로그 파일에만 남습니다. 단계가 실패하면 거기서 중단하고 stderr
꼬리를 보여주므로, 문제 해결 후 `python3 -m gonggu.daily --from <단계>`로 이어서 실행하면
됩니다(`--only <단계>`로 한 단계만, `--list`로 순서 확인). 동시성 기본값(CONCURRENCY=200 등)은
환경변수로 덮어쓸 수 있습니다. lockfile로 이중 실행도 막아줍니다.

아래는 같은 순서의 단계별 수동 실행 목록 — "모듈 하나씩 뜯어보기" 1~9번을 이 순서 그대로,
하루에 한 번 실행하면 됩니다. 6번이
5번보다 먼저 와야 그날 "진행중" 상태가 먼저 확정되고, 7번이 그걸 기준으로 재탐색 대상을
고를 수 있습니다. 9번(`backfill_period.py`)도 6번이 그날 상태를 먼저 확정해야 대상이
정확해지므로 6번 다음에 두는 걸 권장합니다. 8번(`fetch_yt_ppl.py`/`classify_yt_ppl.py`)은
나머지 전부와 완전히 독립이라 순서상 어디에 둬도 무방하지만, "원본 수집"/"LLM 분류"라는
성격이 같은 1번/2번 바로 다음에 각각 두는 걸 권장합니다(두 LLM이 직렬로 도는 효과 —
`classify.py`가 끝나야 `classify_yt_ppl.py`가 시작됨).

```bash
python3 -m gonggu.update_gonggu_stage                        # 6. 공구 상태(시작전/진행중/종료) 갱신
DAYS_BACK=7 python3 -m gonggu.fetch_source                   # 1. 원본 수집
DAYS_BACK=7 python3 -m gonggu.fetch_yt_ppl                   # 8-1. 유튜브 PPL 원본 수집(독립 모듈)
CONCURRENCY=24 python3 -m gonggu.classify                    # 2. LLM#1 공구 분류
CONCURRENCY=100 python3 -m gonggu.classify_yt_ppl             # 8-2. 유튜브 PPL 공구 판별(독립 모듈)
python3 -m gonggu.transform                                  # 3. 보수적 게이트링
RESOLVE_CONCURRENCY=30 python3 -m gonggu.resolve_links          # 4. 링크 해석
python3 -m gonggu.load                                         # 5. DB 적재
python3 -m gonggu.rescan_inprogress                           # 7. 진행중인데 못 찾은 링크 재탐색
python3 -m gonggu.backfill_period                             # 9. 공구기간 판단불가 건 보강 크롤링
```

**한 단계가 끝나야 다음 단계로 넘어갑니다** — 각 스크립트는 그 시점 데이터 전체를 처리하고
끝나므로, 순서대로 하나씩 실행하세요. `CONCURRENCY`/`RESOLVE_CONCURRENCY` 숫자는 본인 컴퓨터
사양에 맞게 조절할 것(너무 높으면 메모리 부족으로 시스템이 느려질 수 있음 — "모듈 하나씩
뜯어보기"의 4번 항목 참고). **같은 단계를 두 터미널에서 동시에 실행하지 마세요** — classify.py/
resolve_links/load.py 모두 동시 실행 시 문제가 생긴 전례가 있습니다.

### 한 번에 자동으로 (1~5번만)

`run_pipeline.py`가 1~5번을 정해진 순서로 이어 부르는 오케스트레이터입니다. **6·7·8·9번(공구
상태 갱신, 진행중 재탐색, 유튜브 PPL 공구 판별, 공구기간 보강)은 아직 포함되어 있지 않으므로
따로 실행해야 합니다.**

```bash
python3 -m gonggu.run_pipeline                              # 이미 fetch했다는 전제, 1~5단계 순서대로
FETCH_FIRST=1 python3 -m gonggu.run_pipeline                 # 원본부터 새로 가져오는 것부터 시작
FETCH_FIRST=1 DAYS_BACK=14 python3 -m gonggu.run_pipeline    # 최근 14일치로 새로 가져오기
python3 -m gonggu.run_pipeline --skip-resolve                # 링크 해석 건너뛰고 원본 후보로 바로 load
python3 -m gonggu.run_pipeline --skip-load                   # DB에 안 넣고 03_load_ready까지만 확인
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

- `gonggu/check_db.py` — 소스/타겟 DB 연결과 타겟 테이블 스키마를 확인하는 점검 스크립트.
- `gonggu/_diag_sample.py` — 링크 해석 품질을 점검하고 싶을 때 쓰는 진단용 스크립트. 실제
  파이프라인 체크포인트(02_classified/03_load_ready 등)는 건드리지 않고, `01_raw`에서 랜덤
  샘플을 뽑아 classify→transform→resolve_links를 돌려서 `data/output/_diag_result.json`에
  남긴다(포스트 원문·프로필 소개글·LLM들의 판단 근거까지 다 같이 저장되어 있어서 결과를 사람이
  직접 하나씩 읽고 판단하기 좋음).
  ```bash
  python3 -m gonggu._diag_sample            # 포스트 300개 랜덤 -> 후보 있는 상품 50개 랜덤
  python3 -m gonggu._diag_sample 500 80     # 포스트 500개, 상품 80개
  ```
- `python3 -m gonggu.maintenance` — 데이터 하우스키핑(2026-08-05, 대공사 3단계). append-only
  체크포인트(link_resolution/period_backfill) 컴팩션(같은 key의 옛 줄 제거 — last-wins 규약이라
  의미 보존), 30일 지난 llm_usage 월별 로테이션, 그리고 `ARCHIVE_AFTER_DAYS=30`처럼 지정한
  경우에만 오래된 01/02 날짜 파일을 `data/archive/`로 gzip 이동. `gonggu.daily`가 마지막
  단계로 자동 실행하며, resolve/rescan 실행 중에 단독으로 돌리지만 말 것.
- `gonggu/_backfill_collapse_candidates.py` — 일회성 백필. 2026-07-29 결정("DB의
  `candidate_url`엔 항상 링크 1개만") 이전에 이미 적재되어 세미콜론으로 여러 후보가 남아있는
  기존 행을 한 번 훑어서 대표 URL 1개로 정리한다. 일일 파이프라인에는 포함하지 않음 —
  다 정리되면 다시 쓸 일 없는 임시 스크립트.

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
