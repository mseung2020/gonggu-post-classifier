# 공구왕 포스트 분류 파이프라인

인스타그램/유튜브 원본 데이터(hifen DB)에서 "확실한 공구"만 최대한 보수적으로 걸러내
플랫폼별 테이블(dev_gongguking DB의 `gonggu_post`/`gonggu_post_product` — 인스타그램,
`gonggu_video`/`gonggu_video_product` — 유튜브)에 저장하는 파이프라인입니다.

**범위: 링크를 "하나로 확정"하는 것까지.** 그 확정된 링크를 실제로 열어서 가격/이미지/옵션
등 진짜 상품 데이터를 가져오는 것은 이 테이블을 읽어가는 별도 개발자의 담당이며, 이 저장소에는
포함되지 않습니다. 전체 그림은 아래 다이어그램을 참고하세요.

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

### 저장소 구조 (2026-08-20 정리)

45개가 넘던 톱레벨 평평한 모듈을 책임별 하위 패키지로 나눴습니다:

| 위치 | 무엇 | 예 |
|---|---|---|
| `gonggu/pipeline/` | daily.py가 순서대로 부르는 본줄기+보강 단계 | `classify.py`, `resolve_links/`, `uc_gate.py` |
| `gonggu/enrich_detail/`, `gonggu/linkbio_parser/` | 이미 책임별로 나뉜 기존 하위 패키지(안 옮김) | — |
| `gonggu/category/` | daily와 무관한 독립 서브파이프라인(LLM#4 제품 카테고리 분류, 수동 실행) | `classify_category.py` |
| `gonggu/infra/` | 여러 단계가 공유하는 배관 — 절대 단독 실행 안 함 | `common.py`, `crawl_pool.py`, `uc_engine.py` |
| `gonggu/tools/` | 사람이 필요할 때 돌리는 진단/유지보수 도구 | `unresolved_board.py`, `llm_usage_report.py` |
| `scripts/` (저장소 최상위, `gonggu/` 밖) | 진짜 일회성 — 스키마 마이그레이션, 임시 진단(대부분 이미 반영됨, 재실행 거의 없음) | `_migrate_detail_blocked.py` |

**실행 명령은 하나도 안 바뀝니다.** `gonggu/classify.py` 같은 옛 톱레벨 경로에는 얇은 호환
shim이 남아 있습니다 — `python3 -m gonggu.classify`, `from gonggu.classify import X`,
pyproject.toml의 `gonggu.classify:main` 진입점이 전부 예전 그대로 동작합니다(shim이
`sys.modules`를 실제 위치의 모듈로 바꿔치기하는 방식이라 `import *`와 달리 밑줄로 시작하는
내부 이름까지 안전합니다 — `tests/test_compat_shims.py`가 이 계약을 못박아 둡니다). **새
코드는 옛 경로(shim)가 아니라 새 하위 패키지에 쓸 것.**

**실행 규약(2026-08-05 패키지화, 2026-08-20 하위 패키지 정리)**: 모든 모듈은 저장소 루트에서
`python3 -m gonggu.<모듈>`로 실행합니다(예: `python3 -m gonggu.classify`,
`python3 -m gonggu.resolve_links`). `scripts/`의 일회성 스크립트도 같은 이유로
`python3 -m scripts.<이름>`으로 실행합니다(`python3 scripts/x.py`처럼 경로로 직접 실행하면
실행 위치에 따라 import가 갈라지는 문제가 있어 2026-08-05에 이미 한 번 걷어냈던 방식입니다 —
`scripts/`를 부활시켰다고 그 문제까지 부활한 건 아닙니다). `pip install -e .`를 한 번 해두면
어느 디렉터리에서든 `gonggu-classify` 같은 짧은 명령으로도 실행할 수 있습니다(pyproject.toml
참고). 코드를 고친 뒤에는 `python3 -m pytest`로 골든 diff를 확인합니다.

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

### 2. `classify.py` — LLM#1 공구 분류

- **무엇**: 01_raw의 각 포스트를 LLM#1(DeepSeek, 프롬프트는 `gonggu/infra/prompts.py`의
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
  RESOLVE_CONCURRENCY=16 python3 -m gonggu.resolve_links   # 데일리 기본값 16
  python3 -m gonggu.resolve_links 50   # 50건만 끊어서 테스트
  ```
- **알아둘 점**:
  - **대기 최적화(2026-08-05, 4단계)**: 상품 사이 3초 대기(ITEM_DELAY)는 이제 "이번 상품에서
    실제로 브라우저를 쓴 경우"에만 적용됩니다(requests 패스트패스/캐시로 끝난 건은 안 쉼 —
    네이버 계열은 항상 브라우저 경로라 항상 대기 유지). 차단율이 이상해지면 `ITEM_DELAY_SMART=0`
    으로 끄면 예전처럼 매 상품 대기로 돌아갑니다. LLM 꼬리 지연이 심하면
    `LINK_LLM_TIMEOUT=45 LINK_LLM_TIMEOUT_RETRY=1`(옵트인 — 재시도라 답이 달라질 수 있음)도
    선택지입니다(gonggu/resolve_links/config.py 주석 참고).
  - **⚠ `RESOLVE_CONCURRENCY`는 Tier1(브라우저 패스) 전용입니다** — Tier0(브라우저 없는 빠른
    패스)는 `RESOLVE_FAST_CONCURRENCY`(200)를 따로 씁니다. 그래서 이 값을 브라우저 수보다 크게
    잡으면 이득이 없고 손해만 납니다: 워커는 **큐에서 항목을 먼저 꺼낸 뒤** 브라우저 허가증을
    기다리므로, 동시에 "붙잡힌" 항목 수가 `MAX_BROWSERS`가 아니라 `RESOLVE_CONCURRENCY`가 됩니다.
    2026-08-19 실측(같은 꼬리 163건, 연속 비교):

    | 설정 | 결과 |
    |---|---|
    | `RESOLVE_CONCURRENCY=60` | 240초에 완료 3건 / 진행 중 60건 → 재기동 때 60건 폐기. 9분에 9건(≈1건/분) |
    | `RESOLVE_CONCURRENCY=16` | 7분 22초에 120건(**≈16.3건/분**), 재기동 0회 |

    워커마다 Playwright 드라이버 프로세스가 하나씩 붙어서 메모리도 같이 먹습니다 — 60개일 때
    16GB 맥의 스왑 여유가 933MB까지 떨어졌습니다. 데일리 기본값은 **16**(`MAX_BROWSERS=14`보다
    살짝 위 — 경합은 없애되 브라우저가 노는 순간은 메움)이고, 지정 안 하면 `config.py`가 이 컴퓨터
    RAM에 맞춰 자동으로 상한을 잡습니다(RAM÷1.5, 4~16 범위 — 16GB면 10).
  - **브라우저 풀 정기 재기동과 드레인**: 오래 재사용한 브라우저 풀은 시간이 지나면 눈에 띄게
    느려져서(메모리 누적), 데일리는 `CRAWL_RECYCLE_SEC=900`으로 15분마다 프로세스를 스스로 끝내고
    풀을 새로 띄웁니다 — daily가 이 종료(exit 4)는 실패로 안 세고 무제한 이어서 재개합니다.
    2026-08-19에 여기에 **드레인**을 붙였습니다: 예전엔 `os._exit`로 즉사시켜서 "큐에서 꺼내
    처리 중이던" 항목이 체크포인트에 못 남고 크롤·LLM 비용을 다 치른 채 버려졌습니다. 지금은
    재기동 시각이 되면 **새 항목 공급만 끊고 진행 중인 건은 마치게 한 뒤**
    (`CRAWL_RECYCLE_DRAIN_SEC`, 데일리 180초) 종료합니다. 유예 안에 안 끝난 워커(먹통 드라이버)는
    예전처럼 버리고 나가며, 종료 메시지가 "재작업 없음"인지 "N건 다시 처리"인지로 어느 쪽인지
    알려줍니다. `0`으로 두면 예전 동작(즉시 종료).
    - ⚠ **드레인만으로는 부족합니다.** in-flight가 `RESOLVE_CONCURRENCY`만큼 쌓이는 구조라, 60일
      때는 유예 90초 동안 완료가 **0건**이었습니다. 위 동시성 조정이 짝으로 있어야 값을 합니다.
    - ⚠ **900초는 아직 완전히 검증된 값이 아닙니다** — 2026-08-19 실측 실행은 첫 재기동 전에
      물량이 끝나서 15분짜리 사이클의 메모리 열화를 못 봤습니다(예전 240초는 "5분쯤 지나면
      느려진다"는 관측에서 나온 값). 대량 배치에서 후반 속도가 떨어지면 이 값을 먼저 의심하고
      400~600으로 낮춰보세요.
  - **링크모음 판별은 두 층입니다(2026-08-19 정리)**. 1층은 도메인 대조(공짜), 2층은 페이지를
    열어 LLM#3에게 "이 페이지 뭐야?"를 묻는 것(비쌈 — 페이지 열기 1회 + LLM 1회).

    | 층 | 목록 | 하는 일 |
    |---|---|---|
    | 1-A | `linkbio_parser/hosts.py` | 구조화 파서 있음 → **브라우저 없이** 후보 추출 |
    | 1-B | `config.KNOWN_HUB_HOSTS` | 파서는 없지만 허브인 건 확실 → 브라우저는 쓰되 **LLM#3 홉을 건너뜀** |
    | 2 | (그 외) | 열어보고 LLM#3에게 물어봄 |

    두 목록 모두 **접미사 매칭**입니다 — `jiy1067.linkstory.co.kr`처럼 계정마다 서브도메인이
    다른 서비스를 잡으려면 완전 일치로는 불가능합니다. 예전엔 완전 일치라서 이런 서비스를
    구조적으로 하나도 못 잡았고(실측: `linkstory.co.kr` 서브도메인 15종, `tuk.link` 6종),
    `link.inpock.com`처럼 TLD만 다른 변형도 샜습니다. **목록에는 등록 도메인만 넣으세요.**
  - **미등록 허브는 이력에서 캐냅니다**: `python3 -m scripts._diag_unknown_hubs` — LLM#3가
    "링크모음"이라 판정했는데 위 목록에 없는 호스트를 등장·실패율·서브도메인 수와 함께 뽑아
    추가 후보를 골라줍니다. 판단이 세 가지로 나뉩니다:
    - `★ 추가 후보` — DOM 추출이 잘 되는 곳. `KNOWN_HUB_HOSTS`에 넣으면 LLM#3 홉을 아낍니다.
    - `추가 금지(열어도 빈손)` — 실패율 90% 이상. 넣어봐야 브라우저만 쓰고 빈손입니다
      (예: `page.im`은 소유자 편집 화면이 렌더링돼서 애초에 긁을 링크가 없습니다).
    - `LLM#3 오분류(허브 아님)` — `cafe.naver.com`(75회)·`open.kakao.com`(20회)처럼 허브가
      아닌데 그렇게 불린 것들. 절대 넣으면 안 됩니다.
  - **브라우저 허가증 유휴율**: 실행이 끝나면 `브라우저 허가증 점유: 총 N초 중 실제 브라우저
    작업 M초 — 유휴 X%`가 찍힙니다. 한 상품은 `브라우저 → LLM#3 → 브라우저 → LLM#2 → …`를
    오가는데 허가증(`MAX_BROWSERS`개)은 그 전체 구간 동안 잡혀 있어서, LLM을 기다리는 동안에도
    14개뿐인 허가증 하나가 묶입니다. 이 비율이 높으면 "크롤 단계와 LLM 단계 분리"가 값을 하고,
    낮으면 지금 구조로 충분합니다. ⚠ **LLM 직전에 허가증만 놓는 단순 처방은 답이 아닙니다** —
    허가증은 곧 살아있는 크롬 수라 놓으려면 닫아야 하고 재기동에 3.9초가 듭니다(무조건 넘기기가
    1.8배 느렸다는 실측: `browser.LazyPage.release_if_contended` 주석).
  - **쿠팡/알리익스프레스/테무 원천 제외(2026-08-11 정책)**: 이 세 마켓플레이스는 공구 대상이
    아니라 후보 단계에서 걸러내고, 리다이렉트형 제휴링크라 입력을 통과해도 최종 도착지가 이 셋이면
    `unresolved`로 뒤집습니다 — done이 되지 않습니다("매일 돌리는 순서" 아래 정책 항목 참고).
  - **`MAX_PER_DOMAIN`**(기본 4): 같은 목적지 도메인(스마트스토어 등)에 동시에 몰리는 걸
    막는 상한 — `browser.py`의 `fetch()`/`redirect.py`의 `follow_redirect()`가 실제로
    페이지를 여는 그 순간에 도메인 기준으로 게이팅합니다(후보 링크의 "첫 번째" 도메인이 아니라
    "실제로 여는" 도메인 기준이라, 인포크처럼 가벼운 1차 홉은 이 제한에 안 걸리고 무거운
    2차 홉(실제 쇼핑몰)만 제대로 보호됨).
  - **인포크 등 링크인바이오 캐시**: 같은 인플루언서 계정을 형제 상품 여러 개가 공유하는
    경우가 많아서(실측: 평균 2.7배 중복), 같은 URL은 프로세스 안에서 한 번만 실제로 요청하고
    재사용합니다.
  - **링크인바이오 파싱본·이메일 저장(2026-08-11)**: 실행이 끝나면 이번에 실제로 파싱된
    링크인바이오 허브(인포크뿐 아니라 `linkbio_parser`가 지원하는 플랫폼 전체 — 링크트리,
    litt.ly 등)를 재크롤 없이 캐시에서 꺼내 `data/linkbio/<게시일>.jsonl`에 저장합니다
    (예전엔 이 저장을 별도 `crawl_linkbio.py`가 담당했는데 지금은 흡수됨, 9-2번 참고). 그
    파싱본 안에서 크리에이터 연락 이메일이 보이면 곁다리로 같이 찾아서(`linkbio_parser.
    extract_emails`) 인스타그램 계정(ig)이면 `data/output/hifen_emails.jsonl`에도
    남깁니다 — `dev_gongguking`엔 이메일 컬럼이 없어 그쪽엔 영향이 없고, hifen DB에 실제로
    반영하려면 별도로 `python3 -m gonggu.sync_hifen_emails`를 돌려야 합니다(9-3번 참고).
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
  - **존재확인 일괄 조회(2026-08-20)**: 이 단계는 매일 `04_resolved` **전체**(누적)를 훑으며
    그날 새로 생긴 것만 넣는 구조라, 대부분이 "이미 있음" 확인에만 쓰이는 DB 왕복이었습니다.
    실측: **45,566건 중 43,737건(96%)이 스킵**이었고 존재확인 1건이 6.06ms라 **그 왕복만 약
    276초**. 지금은 시작할 때 자연키 전체를 한 번에 읽어(**0.12초**) 메모리에서 확인합니다.
    소배치 커밋이 *커밋* 왕복을 1/50로 줄였는데 이 *확인* 왕복은 건당 1회로 남아 있었고,
    그게 이 단계 시간의 거의 전부였습니다.
    - 경합 안전성은 그대로입니다 — 메모리 집합은 왕복을 줄이는 캐시일 뿐이고, 최종 방어선은
      DB의 `UNIQUE(post_id/video_id)`와 그 충돌(errno 1062)을 스킵으로 처리하는 기존 로직입니다.
    - 같은 실행 안에서 넣은 키는 집합에 바로 반영하고, 배치가 롤백되면 집합도 배치 시작
      시점으로 되돌립니다(안 그러면 롤백된 건을 영영 스킵합니다).
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
- **NULL stage 정상화(2026-08-19 추가)**: `_compute_stage`는 절대 NULL을 안 줍니다(날짜가 둘 다
  없으면 `판단불가`). 그런데 실측에서 `gonggu_stage`가 NULL인 상품이 **1380건** 있었고,
  `created_at`이 전부 2026-07-21~08-07에 몰려 있었습니다 — 기간/스테이지를 상품 단위로 옮기던
  시기(2026-08-06)의 잔재로 보입니다. 이 행들은 **어느 단계도 안 보는 사각지대**였습니다:
  - 여기: `gonggu_stage != '종료'`가 NULL 앞에서 NULL(=거짓)이라 선택 자체가 안 됐고, 설령
    고쳐도 두 번째 조건(날짜 둘 중 하나는 있어야 함)에서 또 빠졌습니다.
  - `backfill_period`는 `판단불가`만, `rescan_inprogress`는 `진행중`만 봅니다.

  이제 `gonggu_stage IS NULL` 팔을 추가하고 비교를 NULL-safe(`<=>`)로 바꿔서 `판단불가`로
  정상화합니다(드라이런: 1380건 전환). 그러면 `backfill_period`가 기간을 찾고 → stage가 제대로
  서고 → `rescan_inprogress`까지 이어집니다. 그중 1032건이 링크도 아직 `unresolved`입니다.
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
- **이 단계는 브라우저 바운드입니다**(실측 2026-08-20: 처리 225건 중 브라우저 없이 끝난 건
  **24%**뿐. resolve 첫 사이클은 74%였습니다). 대상이 "이미 한 번 실패한 건"이라 재검증이
  몰리기 때문입니다.
  - **설정 정합(2026-08-20)**: 이 단계는 resolve와 같은 `resolve_product`를 쓰는데 resolve에서
    튜닝한 값들이 안 옮겨져 있었습니다. `RESCAN_CONCURRENCY` 10→**16**(워커 10개로는 브라우저
    14개를 채우지도 못했습니다 — 실측: 크롬 프로세스 41개 ≈ 브라우저 10개, 허가증 4개가
    유휴), `ITEM_DELAY` 3초(기본)→**0**, `LINK_LLM_TIMEOUT` 120초(기본)→**45+재시도1회**.
    같은 날 같은 큐(2,871건)로 연속 비교: **31.3건/분 → 38~43건/분(+30~37%)**. 차단율은
    거의 무변화(0.6%→0.8%)라 `ITEM_DELAY=0`의 안티봇 리스크는 실측상 없었습니다. 대신
    에러율이 0.9%→7.0%로 올랐는데(LLM 타임아웃 단축 + 동시성 증가가 겹친 효과), `error`는
    다음 날 백오프 무시하고 무조건 재시도되니 손실이 아니라 지연입니다.
  - **`MAX_BROWSERS=14`**: 예전엔 이 값을 안 줘서 `config.py`의 RAM 자동계산(16GB÷1.5)이
    **10**으로 떨어졌습니다 — 브라우저 바운드인 이 단계가 정작 "14가 자동계산보다 7.6% 빠르다"는
    실측의 이득을 못 받고 있었습니다. resolve와 같은 값으로 맞췄습니다.
  - **`CRAWL_RECYCLE_SEC=900` + `DRAIN=180`**: 예전엔 재기동이 꺼져 있어 수천
    건을 한 프로세스로 쭉 돌았고, "오래 재사용한 브라우저가 느려지는" 문제에 그대로 노출됐습니다.
    ⚠ 다만 이 값은 resolve 기준으로 정한 값이고, resolve는 브라우저 사용 11%인데 rescan은
    76%라 재기동 한 번의 대가(브라우저 14개를 전부 다시 띄움)가 더 큽니다. 실측 없이 그대로
    가져온 값이니, 재기동 빈도/폐기 건수를 보고 rescan 전용 값(예: 1800초)이 필요한지 확인할 것.
  - **uc 패스 소유 물량 제외(2026-08-20)**: resolve가 네이버/오픈마켓을 만나면 브라우저 없이
    `재검증 중 차단 — uc 패스 대상` 노트로 넘기는데, rescan은 `RESOLVE_UC`를 안 켜므로 같은
    fast-skip에 또 걸려 **한 글자도 다르지 않은 노트**를 다시 씁니다(실측: 후보 풀의 18.3%,
    처리 물량의 14.8%). 속도보다 정확성 문제였습니다 — 이 no-op이 백오프 시도 횟수를 태워서,
    uc만 풀 수 있는 건이 rescan에서 헛시도로 **은퇴**해버렸습니다. 이제 이 노트는 rescan
    대상에서 빠지고 `reverify_uc`가 전담합니다(`is_uc_owned`). 대상 −642건(−26%), done 비율
    7.4%→22.7%로 상승(제외한 물량이 원래 done이 될 수 없던 것들이었으므로).
  - **백오프 `1,2,4,7` → `1,2`로 축소(2026-08-20)**: 회차별 성과를 실측하니(이력 8,570건)
    3회차부터 절벽이었습니다 — 1회 23.1% / 2회 12.1% / **3회 2.4%** / 4회 1.8%.
    `backfill_period`에서 본 것과 같은 모양(1회 17.8%→2회 0.8%→3회 0.1%)이라 같은 결론을
    적용했습니다. 유입이 구조적(공구 시작일 기준 하루 400~600건이 꾸준히 진행중 전환)이라
    백오프 파이프라인이 다 차면 정상상태 대상이 하루 약 1,850건까지 늘어나는데, `[1,2]`면
    약 1,210건(−35%)으로 줄어듭니다. 잃는 건 3·4회차 성과(시도 2,747건당 done 65건, 2.4%)뿐.
    ⚠ backfill의 "소스가 안 바뀌면 재시도 안 함"과는 다릅니다 — 여기는 판매자가 링크를 늦게
    올릴 수 있어 시간이 지나면 답이 실제로 바뀔 여지가 있고(그 여지가 위 2.4%), 완전히 0은
    아닙니다. 더 보수적으로 가려면 `RESCAN_BACKOFF_DAYS=1,2,4`(4회)로.
  - Tier0/Tier1 분리(resolve에는 있고 여기엔 없음)는 아직 안 넣었습니다 — 브라우저 없이 끝나는
    비율이 24%뿐이라 추정 이득이 20~25%로, 코드 변경 규모 대비 우선순위가 낮습니다. 위 설정
    정합·소유권 정리·백오프 축소를 먼저 실측하고 결정하세요.
  - **진단 추가(2026-08-20)**: resolve와 같은 `_print_resolution_diagnostics`를 붙여서, 이제
    이 단계도 실행 끝에 경로 분해(브라우저 실사용 비율)와 허가증 유휴/대기가 찍힙니다. 그전엔
    이 단계 튜닝이 전부 추정으로 갔었습니다.
- **`RESCAN_UNKNOWN_STAGE_DAYS`(기본 0=끔) — 판단불가 교착 풀기(2026-08-19)**: 이 단계의 문은
  `gonggu_stage='진행중'`인데, 거기 못 들어오는 고리가 있습니다.

  ```
  링크를 찾으려면        → rescan이 필요한데, rescan은 stage='진행중'만 봄
  stage가 진행중이 되려면 → 기간을 찾아야 하는데
  기간을 찾으려면        → backfill_period Tier1(몰 크롤)이 필요한데, 그건 link_status='done'만 봄
  그런데 링크는 아직 unresolved  ← 처음으로 돌아감
  ```

  빠져나갈 길이 backfill Tier0(인포크 텍스트) 하나뿐이라, 그게 없거나 실패하면 아무도 다시
  안 건드립니다. 실측(2026-08-19): `unresolved`+`hold` 27518건 중 rescan이 보던 건
  **1643건(6%)**뿐이고, 판단불가에 갇힌 게 **9827건**이었습니다.

  > 같은 조사에서 `gonggu_stage`가 **NULL**인 상품 1380건도 나왔습니다 — 판단불가보다 더 깊은
  > 사각지대(어느 단계도 안 봄)였고, `update_gonggu_stage`가 이제 `판단불가`로 정상화합니다.
  > 6번 항목 참고.

  이 값을 N일로 두면 `stage='판단불가'`이면서 최근 N일 내 발행된 상품도 대상에 넣습니다.
  기간 제한을 두는 건 오래된 공구는 링크를 찾아도 가치가 떨어지기 때문인데, 실측상 이 풀은
  의외로 신선합니다 — 7일 이내 18%, **30일 이내 누적 89%**, 90일 초과 0건.

  ⚠ **켜는 첫 실행은 크게 돕니다.** 실측으로 오늘 대상이 1849 → **10594건**(+8745)이 됐습니다.
  그 물량이 전부 "신규전환"으로 잡히기 때문입니다. `LIMIT`으로 며칠에 나눠 흘리세요 — 그 뒤로는
  백오프 스케줄이 알아서 떨어뜨립니다.

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

### 9. `backfill_period.py` — 공구기간 보강 (매일 보강)

- **무엇**: 캡션에 명시적 날짜가 없어 상품 `gonggu_stage='판단불가'`로 남은 상품의 기간을
  채웁니다. 2026-08-18에 옛 별도 스크립트(`backfill_period_inpock.py`)를 이 파일 안의
  2단 에스컬레이션으로 병합했습니다:
  - **Tier0(인포크 텍스트, 크롤 없음)**: 그 포스트의 인포크 허브 파싱본
    (`data/linkbio/<날짜>.jsonl`, `resolve_links`가 남김)에 기간이 있을 만한 텍스트가 있으면
    브라우저 없이 바로 LLM(`prompts.PERIOD_BACKFILL_SYSTEM`)에 태웁니다. 빠르고 안티봇도 안
    탑니다.
  - **Tier1(몰 크롤)**: Tier0에서 못 찾았고, 그 상품 링크가 이미 `link_status='done'`으로
    확정된 경우에만 그 확정 상품페이지를 크롤링해서 같은 LLM으로 다시 찾습니다.
  - 어느 티어든 찾으면 그 상품의 `gonggu_start_date`/`gonggu_end_date`와 `gonggu_stage`를
    그 자리에서 같이 갱신합니다. 인포크 텍스트도 없고 링크도 아직 미확정이면(검사할 소스
    자체가 없음) 이번 실행에서는 건드리지 않습니다(재시도 횟수도 안 늘림).
- **입력/출력**: `gonggu_post_product`/`gonggu_video_product` 테이블 자체 + `data/linkbio/`
  (Tier0 입력, 파일 관여) + `data/output/period_backfill.jsonl`(두 티어 공통 체크포인트 —
  찾았으면 영구 스킵, 못 찾았으면 `PERIOD_RETRY_COOLDOWN_DAYS`일 쿨다운 후 재시도,
  `PERIOD_MAX_ATTEMPTS`회 넘으면 영구 스킵). 병합 전에는 인포크 쪽만 "한 번 못 찾으면 영구
  스킵"이라는 별도 정책이었는데, 이제 두 티어가 하나의 체크포인트·정책을 공유합니다.
- **명령**: `python3 -m gonggu.backfill_period` (`LIMIT=20`으로 소규모 테스트,
  `PERIOD_INPOCK_CONCURRENCY`로 Tier0 동시성(daily 기본값 **150**), `BACKFILL_PERIOD_CONCURRENCY`로
  Tier1 동시성(daily 기본값 **14**) 조절 — 두 값이 별도인 이유는 resolve_links의
  `RESOLVE_FAST_CONCURRENCY`/`RESOLVE_CONCURRENCY` 구분과 같습니다: Tier0는 브라우저가 없어
  훨씬 높게 잡을 수 있습니다)
  - **동시성 재조정(2026-08-20)**: 예전 값(Tier0 40 / Tier1 8)이 둘 다 너무 낮았습니다.
    Tier0는 브라우저를 안 쓰는 순수 LLM 호출인데(`use_playwright=False`) 딥시크 동시 상한
    (flash 2500)에 비하면 40은 한참 아래였습니다 — 같은 실행에서 재시작 전후로 실측:
    **609건/분 → 1,566건/분(150으로, 2.6배)**, 그동안 크롬 0개·RAM 여유라 병목이 아니었습니다.
    Tier1은 반대로 걸려 있었습니다 — 워커 8개가 `MAX_BROWSERS` 자동계산(10)도 못 채워서
    (실측: 크롬 프로세스 33개 ≈ 브라우저 8~9개) 48건/분에 머물렀습니다. `MAX_BROWSERS=14`를
    명시하고 워커를 resolve/rescan에서 검증된 비율(≈1.14×)로 14로 올렸습니다.
    ⚠ 예전 경고("`BACKFILL_PERIOD_CONCURRENCY=40` 금지 — 크롬10 초과예약 churn으로 1037에서
    멈춤")는 지금 처방과 다른 얘기입니다 — 그건 5배 초과예약(40/8)이었고, 지금은 resolve/rescan과
    같은 소폭 초과예약(14/14, 자동계산이 아니라 명시값)입니다.
- **알아둘 점**: 대상 자체가 상품 stage='판단불가'(그 상품의 시작일/종료일 둘 다 NULL)뿐이라
  이미 날짜가 있는 상품은 조회조차 안 돼 기존 값을 덮어쓸 여지가 구조적으로 없습니다.
  기간/스테이지가 상품 단위로 이전된 뒤(2026-08-06)로는 상품이 2개 이상인 게시물도
  스코프에 포함됩니다 — 예고 달력처럼 다중상품인 게시물의 각 상품도 자기 확정 페이지/인포크
  텍스트에서 기간을 따로 찾습니다.
  `update_gonggu_stage.py`와 마찬가지로 `transform.py`의 `_compute_stage`를 재사용합니다.
- **재시도는 "새 소스가 생겼을 때만"(2026-08-19 정책 전환)**: 예전엔 못 찾으면 쿨다운 5일 뒤
  `PERIOD_MAX_ATTEMPTS`까지 **같은 걸 다시** 읽었습니다. 같은 텍스트를 같은 LLM에 태우면 답도
  같으니 당연히 헛수고였는데, 실측이 그대로 나왔습니다:

  | 시도 | 발견율 |
  |---|---|
  | 1회차 | **17.8%** |
  | 2회차 | 0.8% |
  | 3회차 | 0.1% (1053회 시도에 **1건**) |

  그런데 2회차 이상에서 찾은 35건은 **전부 몰 크롤(상품 페이지)**에서 나왔습니다 — 1회차엔
  링크가 `unresolved`라 없던 소스가 그 사이 생긴 것입니다. 그래서 재시도 기준을 횟수/날짜에서
  **소스 지문**으로 바꿨습니다: `inpock:<내용해시>` / `mall:<URL해시>`를 체크포인트에 남기고,
  거기 없던 지문이 생겼을 때만 다시 봅니다. 인포크 텍스트가 바뀌는 경우(크리에이터가 나중에
  기간을 적어 넣음)도 내용 해시라 자동으로 잡힙니다. `PERIOD_MAX_ATTEMPTS`는 폭주 방지
  상한으로만 남습니다.
  - `sources`가 없는 **옛 기록은 예전 규칙(쿨다운+횟수)을 그대로** 씁니다 — 안 그러면 7481건이
    한꺼번에 재시도 대상이 됩니다. 손해도 없습니다: 옛 규칙으로 이미 영구 스킵된 1052건 중
    지금 몰 소스가 생긴 건이 **0건**이었습니다.
  - 절감은 **다음 실행부터** 나타납니다. 이번 실행이 지문을 남겨야 그다음부터 걸러집니다.

### 9-2. `crawl_linkbio.py` — 링크인바이오 허브 크롤 (백로그 소급 전용, standalone)

- **무엇**: 우리가 수집한 공구 포스트/영상의 캡션·프로필에서 링크인바이오 허브 URL을 찾아
  (인포크뿐 아니라 `linkbio_parser.hosts`가 지원하는 플랫폼 전체 — 링크트리, litt.ly,
  bio.site 등, 2026-08-11부터 인포크 한정 해제), `linkbio_parser`로 파싱한 정보 전체
  (링크/스토어/상품/텍스트/bio 등)를 게시일별 JSONL로 저장합니다. `/api/r/<토큰>`
  (인포크 버튼 리다이렉트)은 허브가 아니라 개별 상품 링크라 제외합니다.
- **입력/출력**: 대상은 `gonggu_post`/`gonggu_video`(그래서 `load` 이후에 실행), 캡션·프로필은
  hifen(SRC)에서 조회. 출력은 `data/linkbio/<게시일>.jsonl`(포스트별 레코드) +
  `data/linkbio/_hub_cache.jsonl`(허브당 1회만 크롤하는 캐시) +
  `data/linkbio/_processed.jsonl`(스캔한 포스트 체크포인트).
- **명령**: `python3 -m gonggu.crawl_linkbio` (`LIMIT=200` 소규모, `LINKBIO_CONCURRENCY=8`,
  `RESOLVE_INNER=1`이면 허브 내부 `/api/r/` 링크의 최종 주소까지 추적 — 파이프라인 parse와 동일하나 느림)
- **알아둘 점**: DB/파일 상태가 곧 증분 기준이라(idempotent) 첫 실행은 백로그 전수, 이후 실행은
  아직 스캔 안 한 새 포스트만 처리합니다. 다만 데일리(`gonggu.daily`)에는 포함되지 않습니다 —
  4번(`resolve_links`)이 인포크뿐 아니라 링크인바이오 전체를 매일 알아서 흡수해 저장하므로
  (아래 4번 항목·9-3번 참고), 이 스크립트는 그 자동화가 생기기 전(2026-08-11 이전)에 이미
  DB에 있던 옛 포스트를 소급 정리할 때만 씁니다.

### 9-3. `sync_hifen_emails.py` — 크리에이터 이메일을 hifen DB로 반영 (수동, 필요할 때)

- **무엇**: 4번(`resolve_links`)이 링크인바이오 허브를 파싱하는 김에 곁다리로 찾아 로컬 파일
  (`data/output/hifen_emails.jsonl`, 인스타그램 계정별 1줄)에 쌓아 둔 크리에이터 연락 이메일을
  hifen DB의 `instagram_user.email` 컬럼에 반영합니다. 이메일이 여러 개면 쉼표로 이어붙여
  하나의 문자열로 넣습니다.
- **입력/출력**: 입력은 `data/output/hifen_emails.jsonl`(파일만, DB 조회 없음). 출력은
  hifen(SRC)의 `instagram_user.email` UPDATE뿐 — **`dev_gongguking`(우리 자체 DB)에는 이메일
  컬럼이 아예 없고 앞으로도 만들지 않으며, 이 명령이 그쪽 테이블을 건드리는 일도 없습니다.**
- **명령**: `python3 -m gonggu.sync_hifen_emails`
- **알아둘 점**:
  - hifen(SRC)은 이 저장소 전체에서 지금까지 읽기 전용으로만 써왔는데(`common.connect_src`
    참고), 이 명령만 유일하게 예외적으로 UPDATE를 합니다 — 대상도 `email` 컬럼 하나뿐입니다.
  - 별도 "오늘 것만" 체크포인트 없이 매번 파일 전체를 hifen과 다시 비교합니다 — UPDATE 문이
    기존과 똑같은 값을 넣으면 MySQL이 rowcount를 0으로 돌려주므로, 그 rowcount로 "이번에
    실제로 바뀐 것"만 집계합니다. 그래서 몇 번을 다시 돌려도 안전하고(idempotent), 실행할
    때마다 정확히 그 실행에서 새로 반영된 계정 수·이메일 개수만 출력됩니다.
  - `resolve_links`가 자동으로 매일 채워주는 파일을 읽기만 하므로, 이 명령 자체는 크롤링을
    하지 않고 즉시 끝납니다 — 데일리 실행 뒤 아무 때나 따로 돌리면 됩니다.

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
워크플로우 도구에 의존하지 않고, 프롬프트와 호출 로직이 이 저장소 코드(`gonggu/infra/common.py`의
`call_llm`, `gonggu/infra/prompts.py`의 시스템 프롬프트들) 안에 그대로 있습니다. `.env`에
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

**한 번에: `python3 -m gonggu.daily`** (2026-08-05 추가, 2026-08-20 뒷단까지 통합) — 아래 순서
전체를 한 명령으로 실행합니다. 각 단계의 stdout은 콘솔과 `data/logs/daily_<시각>.log`에 같이
남고, stderr(Playwright 노이즈 등)는 로그 파일에만 남습니다. 단계가 실패하면 거기서 중단하고
stderr 꼬리를 보여주므로, 문제 해결 후 `python3 -m gonggu.daily --from <단계>`로 이어서 실행하면
됩니다(`--only <단계>`로 한 단계만, `--list`로 순서 확인). 동시성 기본값(CONCURRENCY=200 등)은
환경변수로 덮어쓸 수 있습니다. lockfile로 이중 실행도 막아줍니다.

**2026-08-20 — 뒷단 보강 3단계가 daily 안으로 들어왔습니다.** 예전에는 daily가 끝난 뒤 이 4줄을
손으로 쳤습니다:

```bash
python3 -m gonggu.sync_hifen_emails
rm -rf ~/.gonggu_uc_profile
python3 -m gonggu.enrich_detail.warmup_naver_uc
LIMIT=100 UC_LOGIN_WAIT=0 REVERIFY_CONCURRENCY=10 python3 -m gonggu.resolve_links.reverify_uc
```

이제 전부 `python3 -m gonggu.daily` 한 줄입니다. 순서는
`… → backfill_period → sync_emails → uc_gate → reverify_uc → maintenance`
(`maintenance`는 그날 쓴 파일을 정리하는 단계라 맨 뒤로 옮겼습니다).

- **`rm -rf`가 사라졌습니다.** 예전에는 매일 uc 신뢰를 버리고 다시 쌓았는데 대부분의 날은
  프로필이 멀쩡해서 낭비였습니다. **uc 게이트**(`gonggu/pipeline/uc_gate.py`)가 먼저 비대화형으로
  "지금 uc로 네이버가 뚫리는가"를 확인하고(`uc_healthcheck.probe` — 창이 잠깐 떴다 닫힙니다)
  살아있으면 그대로 통과, 죽었을 때만 프로필을 초기화하고 워밍업을 띄웁니다.
- **워밍업을 띄울지는 `sys.stdin.isatty()`로 정합니다.** cron/nohup으로 돌리면 stdin이 TTY가
  아니라 입력이 영영 안 오므로 기다리지 않고 uc 단계(`reverify_uc`)만 건너뜁니다 — 나머지는 다
  돕니다. TTY인데 사람이 자리를 비운 경우는 `UC_WARMUP_TIMEOUT_SEC`(기본 600초) 뒤 같은 처리.
- **실패 정책이 단계별로 나뉩니다.** 본줄기(앞 10개)는 뒤 단계가 앞 결과에 의존하므로 예전처럼
  즉시 중단합니다. 뒷단 보강 4개(`sync_emails`/`uc_gate`/`reverify_uc`/`maintenance`)는 서로
  독립이라 실패해도 다음으로 넘어가고, 마지막 요약에 `✗`로 모아 보여줍니다.
- **`--from`/`--only` 이름은 그대로입니다** — `--from resolve_links` 등 예전 명령이 그대로
  동작하는 것을 테스트로 못박아 뒀습니다(`tests/test_daily_stages.py`).
- **`--until <단계>`**(2026-08-20 추가, 그 단계까지 포함) — 하루를 두 번에 나눠 돌 때 씁니다.
  긴 무인 단계를 오프피크에 먼저 돌려두고, 사람이 붙어야 하는 uc 구간은 자리에 있을 때:

  ```bash
  python3 -m gonggu.daily --from rescan_inprogress --until backfill_period   # 1차: 무인, 오프피크
  UC_WARMUP_TIMEOUT_SEC=1800 python3 -m gonggu.daily --from sync_emails      # 2차: uc 게이트가 바로 뜸
  ```

  `--until`이 `--from`보다 앞이면 조용히 빈 목록을 돌려주는 대신 순서가 뒤집혔다고 알려줍니다.

> ⚠ **`reverify_uc`는 큐를 비우는 단계가 아니라 매일 30분씩 갉는 단계입니다.** 2026-08-20 실측 —
> 유입은 resolve 한 번당 하루 약 436건(`로그인월_차단` 69 + `재검증 중 차단` 367)인데, uc는
> `uc_engine._lock`으로 **전역 직렬**이라 동시성을 올려도 페치는 한 번에 하나뿐이고 30분
> 예산의 배수는 건당 15초 기준 120건, 30초 기준 60건, 75초(타임아웃) 기준 24건입니다. 즉 큐는
> 계속 자랍니다(8/19 702건 → 8/20 2,301건). `UC_TIME_BUDGET_SEC=1800`의 역할은 큐 감축이 아니라
> **안전밸브**입니다 — uc가 죽거나 느려도 데일리가 묶이는 최대치를 30분으로 못박습니다(uc는
> 2026-08-12에 대량 무인 경로 반복 크래시로 데일리에서 뺐던 물건이고, 게이트는 쿠키 신뢰만
> 책임지지 크래시 내성을 주지는 않습니다). 실행하면 **대상 / 예산 내 시도 / 큐 잔량+유입 추정**
> 세 숫자를 같이 찍어주니 예산을 언제 늘릴지는 그걸 보고 판단하세요. 진짜 해법은 병렬화입니다 —
> 상세수집이 이미 쓰는 `UC_PROFILE` 샤딩과 같은 패턴을 적용하면 프로필 N개로 N배가 나옵니다
> (건당 실제 소요가 실측된 뒤에 검토).

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
RESOLVE_CONCURRENCY=16 python3 -m gonggu.resolve_links          # 4. 링크 해석(Playwright — uc 아님)
python3 -m gonggu.load                                         # 5. DB 적재
RESCAN_CONCURRENCY=40 python3 -m gonggu.rescan_inprogress     # 7. 진행중인데 못 찾은 링크 재탐색
PERIOD_INPOCK_CONCURRENCY=40 python3 -m gonggu.backfill_period   # 9. 공구기간 백필(인포크 우선 Tier0 → 몰 크롤 Tier1)
python3 -m gonggu.sync_hifen_emails                          # 11. 크리에이터 이메일 → hifen DB
python3 -m gonggu.uc_healthcheck                             # 12. uc 신뢰 점검(daily는 여기서 필요하면 워밍업까지)
UC_LOGIN_WAIT=0 python3 -m gonggu.resolve_links.reverify_uc  # 13. 차단 계열 unresolved를 uc로 재시도
python3 -m gonggu.maintenance                                # 19. 하우스키핑(컴팩션/로테이션)
```

> **`daily`의 본줄기(4·7번)는 네이버/오픈마켓을 uc로 뚫지 않습니다**(2026-08-12) — 대량 무인
> 경로에서 uc가 반복 크래시를 내서 뺐고, 안정적인 Playwright로만 돌립니다. 로그인월/봇확인으로
> 막힌 것은 그 뒤 `reverify_uc` 단계가 uc로 따로 구제합니다(2026-08-20부터 daily 안, 시간 예산
> 30분 · 실패해도 계속 · 게이트 미통과 시 자동 스킵 — 위 통합 설명 참고).

**한 단계가 끝나야 다음 단계로 넘어갑니다** — 각 스크립트는 그 시점 데이터 전체를 처리하고
끝나므로, 순서대로 하나씩 실행하세요. `CONCURRENCY`/`RESOLVE_CONCURRENCY` 숫자는 본인 컴퓨터
사양에 맞게 조절할 것(너무 높으면 메모리 부족으로 시스템이 느려질 수 있음 — "모듈 하나씩
뜯어보기"의 4번 항목 참고). **같은 단계를 두 터미널에서 동시에 실행하지 마세요** — classify.py/
resolve_links/load.py 모두 동시 실행 시 문제가 생긴 전례가 있습니다.

### 네이버/오픈마켓 uc 구제 패스 (2026-08-20부터 daily 안, 손으로도 실행 가능)

데일리의 링크 해석(4·7번)은 안정적인 **Playwright**로만 돌립니다 — 네이버 스마트스토어/블로그·
G마켓·옥션·오하우스·11번가 등은 로그인월/봇확인으로 `unresolved`(note에 "로그인월_차단")로
남는데, 이건 그 뒤 `undetected_chromedriver`(uc) 패스로 따로 구제합니다. uc는 실제 크롬 창
하나를 직렬로 쓰는 무거운 방식이라(2026-08-12에 대량 무인 경로에서 반복 크래시가 확인돼 본
resolve/rescan에서는 뺐습니다) 저동시성 + 시간 예산으로만 씁니다 — **resolve/rescan과 절대
동시에 돌리지 마세요**(daily 안에서는 순차 실행이라 자동으로 지켜집니다).

`python3 -m gonggu.daily`가 `uc_gate` → `reverify_uc` 순서로 이걸 알아서 합니다. 손으로 돌리는
경우:

```bash
python3 -m gonggu.uc_healthcheck                                                    # 지금 뚫리는지 비대화형 점검(항상 exit 0)
python3 -m gonggu.enrich_detail.warmup_naver_uc                                     # 신뢰가 죽었을 때만 — 뜨는 크롬 창에서 로그인/보안확인을 직접 통과(쿠키가 프로필에 남음)
UC_LOGIN_WAIT=0 REVERIFY_CONCURRENCY=6 python3 -m gonggu.resolve_links.reverify_uc  # 차단 계열 unresolved를 uc로 재시도(예산 없이 큐 전체)
UC_TIME_BUDGET_SEC=1800 UC_LOGIN_WAIT=0 python3 -m gonggu.resolve_links.reverify_uc # 30분만(daily가 쓰는 방식)
LIMIT=100 UC_LOGIN_WAIT=0 REVERIFY_CONCURRENCY=6 python3 -m gonggu.resolve_links.reverify_uc  # 100건씩 나눠서
```

`UC_TIME_BUDGET_SEC`의 기본값은 0(예산 없음)이라 손으로 돌리는 위 명령들의 동작은 예전 그대로
입니다 — daily만 1800을 넘깁니다. 예산을 넘기면 남은 건은 **시도하지 않고**(따라서 은퇴
카운트도 안 올라갑니다) 정상 종료하고 다음 실행이 이어받습니다. 큐가 왜 안 줄어드는지는 위
"매일 돌리는 순서"의 유입/배수 설명을 보세요.

- **대상 선정(2026-08-19 확대)** — 예전엔 note에 `재검증 중 차단`이 있는 건만 골랐는데, 그건 fast
  resolve가 브라우저를 아예 생략하고 넘긴 소수(실측 4건)뿐이었고 정작 uc가 뚫으라고 만들어진
  네이버 안티봇 건들은 `로그인월_차단`이라는 다른 문구로 988건이 쌓여 있었습니다. 지금은 두 문구를
  모두 후보로 잡고(992건) 파이썬에서 세 단계로 거릅니다 — 실행할 때 사유별 분포를 찍어줍니다:
  | 단계 | 하는 일 | 실측(2026-08-19) |
  |---|---|---|
  | 차단 계열 note | `재검증 중 차단` 또는 `로그인월_차단` | 992 |
  | 죽은 페이지 제외 | 404·존재하지 않음·오류 페이지·인스타·알리 등은 uc로도 안 풀림 | −203 |
  | uc 대상 판정 | `RESOLVE_UC_HOSTS` 호스트이거나 note가 안티봇/보안확인을 가리킬 때만 | −87 |
  | **최종 대상** | | **702** |
- **은퇴** — uc로 열었는데도 못 풀린 횟수를 `data/output/reverify_uc_state.jsonl`에 쌓고,
  `UC_MAX_ATTEMPTS`(기본 3)를 넘기면 대상에서 뺍니다. `rescan_inprogress`의 백오프/은퇴와 같은
  방식이지만 상한이 훨씬 낮습니다 — 그쪽은 무인이라 재시도가 싸지만 여기는 사람이 곁에 붙는
  패스라 헛시도 하나의 비용이 비교가 안 됩니다. `done`이 되면 애초에 후보 풀에서 빠집니다.

- uc 전용 크롬 프로필은 `~/.gonggu_uc_profile`에 둡니다(2026-08-12) — 이 저장소가 iCloud로
  동기화되는 `Documents` 안에 있어서, 프로필을 저장소 하위에 두면 iCloud가 크롬 락/SQLite 파일을
  실시간 동기화해 크롬이 크래시합니다. `UC_PROFILE=...` 환경변수로 다른 경로를 지정할 수 있습니다.
- 한 항목이 uc에서 `UC_HARD_TIMEOUT`초(기본 75)를 넘겨 먹통이 되면 드라이버를 강제 종료해 다음
  항목으로 넘어갑니다 — 예전처럼 한 창이 wedge돼 몇 시간씩 멈추는 일을 막는 워치독입니다.
- 데일리에 uc를 다시 넣고 싶다면(권장하지 않음) `daily.py`의 resolve_links/rescan_inprogress
  단계 env에 `RESOLVE_UC=1`, `RESOLVE_UC_HOSTS=...`, `UC_LOGIN_WAIT=0`을 넣으면 됩니다(그 파일
  상단 주석 참고).

### 쿠팡/알리익스프레스/테무 원천 제외 (2026-08-11 정책)

이 세 마켓플레이스는 공구 대상이 아니라, resolve 단계에서 아예 `done`이 되지 못하게 막습니다 —
후보 입력 단계에서 걸러내고(`is_excluded_marketplace`), 리다이렉트형 제휴링크라 입력 필터를
통과해도 **최종 도착지**가 이 세 곳이면 다시 `unresolved`로 뒤집습니다(2026-08-12, yt PPL 알리
누수 대응). 이미 DB에 적재된 옛 링크를 청소하려면 일회성으로:

```bash
python3 -m gonggu.purge_marketplace_links            # 미리보기(대상만 출력)
python3 -m gonggu.purge_marketplace_links --yes      # 실제 삭제(--status로 대상 상태 지정 가능)
```

### 한 번에 자동으로 (1~5번만)

`daily.py`의 `--until`로 원하는 단계까지만 돌릴 수 있습니다 — 예전엔 이 용도로 1~5번만 커버하는
별도 오케스트레이터(`run_pipeline.py`)가 있었는데, `daily.py`가 uc/이메일 보강까지 포함한 완전한
상위호환이라 2026-08-20에 지웠습니다.

```bash
python3 -m gonggu.daily --until load                # fetch부터 load까지(옛 run_pipeline과 동등)
python3 -m gonggu.daily --from classify --until load   # 이미 fetch했다는 전제로 이어서
```

`--skip-resolve`/`--skip-load` 같은 부분 스킵은 없습니다 — 대신 `--until transform`(링크 해석 전
원본 후보까지만)이나 개별 `--only <단계>`로 필요한 지점까지만 실행하세요.

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

### LLM 비용 (`llm_usage_report.py`)

`common.call_llm`이 호출마다 `data/output/llm_usage.jsonl`에 토큰 수를 남기고, 데일리 마지막에
`llm_usage_report`가 날짜별·모델별로 집계합니다. 단가는
`llm_usage_report.MODEL_PRICES_PEAK`에 있습니다.

**2026-08-19 DeepSeek 요금 개편 반영** — 두 가지가 바뀌었습니다.

1. **단가 인상.** 옛 값(`input 0.14 / output 0.28 / cache_hit 0.0028`)을 그대로 뒀다면 실제
   비용의 **40%만** 보여줬을 겁니다(실측 2026-08-19 하루: 보고 $10.74 vs 실제 $27.38).
2. **피크/오프피크 도입.** 오프피크는 피크의 **정확히 절반**이고, 피크는
   **01:00-04:00 · 06:00-10:00 UTC** = **10~13시 · 15~19시 KST**입니다.

| $ / 1M 토큰 | flash 오프피크 | flash 피크 | pro 오프피크 | pro 피크 |
|---|---|---|---|---|
| 입력(캐시 히트) | 0.007 | 0.014 | 0.022 | 0.044 |
| 입력(캐시 미스) | 0.22 | 0.44 | 0.66 | 1.32 |
| 출력 | 0.66 | 1.32 | 1.98 | 3.96 |

- 호출 하나하나가 UTC 몇 시에 났느냐로 단가가 2배 갈리므로, 집계를 **(모델 × 피크여부)로 쪼개서**
  계산합니다. 하루치를 모아 단가를 한 번 곱하면 틀립니다.
- 리포트가 피크/오프피크 분해와 **절약 가능액**을 같이 찍습니다. 실측 2026-08-19: 하루 $27.38 중
  피크 구간이 $8.02였고, 그걸 오프피크로 옮기면 $4.01(월 약 $120) 절약입니다. **데일리를
  10시 이전이나 13~15시·19시 이후에 돌리면 그대로 반값**입니다.
- 오프피크 단가는 코드에서 피크를 2로 나눠 만듭니다 — 두 벌을 손으로 적으면 한쪽만 고치는
  드리프트가 생깁니다.
- `ts`에 타임존 오프셋을 붙여 기록합니다(`2026-08-20T09:27:49+09:00`). 그 이전 기록은 타임존
  없는 로컬 시각이라, 리포트가 이 머신의 로컬 타임존으로 해석합니다.
- 동시 호출 상한은 flash 2500 / pro 500입니다 — 지금 쓰는 값(`CONCURRENCY=200`,
  `RESOLVE_FAST_CONCURRENCY=200`)은 한참 아래입니다.

### 보조/진단 스크립트

- `gonggu/check_db.py` — 소스/타겟 DB 연결과 타겟 테이블 스키마를 확인하는 점검 스크립트.
- `gonggu/_diag_unknown_hubs.py` — LLM#3가 "링크모음"이라 판정했는데 도메인 목록엔 없는
  호스트를 체크포인트 이력에서 캐낸다(4번 항목의 `KNOWN_HUB_HOSTS` 갱신용).
- `gonggu/_diag_sample.py` — 링크 해석 품질을 점검하고 싶을 때 쓰는 진단용 스크립트. 실제
  파이프라인 체크포인트(02_classified/03_load_ready 등)는 건드리지 않고, `01_raw`에서 랜덤
  샘플을 뽑아 classify→transform→resolve_links를 돌려서 `data/output/_diag_result.json`에
  남긴다(포스트 원문·프로필 소개글·LLM들의 판단 근거까지 다 같이 저장되어 있어서 결과를 사람이
  직접 하나씩 읽고 판단하기 좋음).
  ```bash
  python3 -m scripts._diag_sample            # 포스트 300개 랜덤 -> 후보 있는 상품 50개 랜덤
  python3 -m scripts._diag_sample 500 80     # 포스트 500개, 상품 80개
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
