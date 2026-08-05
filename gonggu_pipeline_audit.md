# 공구왕 파이프라인 대공사 감사 보고서

작성일: 2026-08-05 · 대상: gonggu-post-classifier 전체(스크립트 24개 + resolve_links/linkbio_parser 패키지 + DDL + data/ 실제 상태)
전제: **판정 결과 완전 보존** · 우선순위: 구조/유지보수 > 데이터 관리 > 속도 · 일일 퀘스트는 며칠 중단 가능

---

## 1. 총평

몇 주간의 실측 기반 수정이 쌓인 티가 나는 코드베이스입니다. 특히 잘 되어 있는 것들은 대공사에서도 **그대로 보존**해야 합니다: append-only JSONL 체크포인트(2026-07-27 성능 문제의 근본 해결), LazyPage + MAX_BROWSERS 허가증 구조(2026-07-30 스왑 32GB 사고의 재발 방지), 실제 목적지 기준 domain_gate, HTTP 패스트패스와 그 폴백 사유 통계, 그리고 "사고 하나 = 방어 규칙 하나"로 대응되는 antibot.py의 설계 철학까지. 주석에 실측 날짜와 근거가 남아 있는 문화도 이 저장소의 큰 자산입니다.

문제는 코드가 "스크립트 모음"에서 "시스템"으로 넘어가는 경계에 서 있다는 점입니다. 같은 패턴(LLM 재시도 루프, 워커 풀, 플랫폼별 SQL 쌍)이 서너 곳에 복사되어 있고, 그중 한 곳에만 적용된 버그 수정이 이미 실제로 발생하고 있습니다(대표 사례가 아래 A1 — rescan에는 적용된 브라우저 안전판이 backfill에는 빠져 있음). 데이터는 매일 15~25MB씩 순증하는데 모든 단계가 "전체 히스토리를 매번 다시 읽고 다시 계산"하는 구조라, 지금은 수 초~수십 초지만 시간이 지날수록 선형으로 느려집니다. 후반부 50%를 이 기반 위에 쌓기 전에 지금 정리하는 것이 맞는 타이밍입니다.

핵심 제안을 한 문장으로 요약하면: **테스트 안전망을 먼저 깔고 → 즉효 수정(버그/위험) → 3대 공통 레이어 추출(LLM 배치 러너 · 크롤링 워커 풀 · 플랫폼 메타테이블) → 데이터 증분화/컴팩션 → 속도 튜닝** 순서로, 각 단계마다 before/after 산출물 diff로 결과 동일성을 검증하며 진행합니다.

---

## 2. 즉시 고칠 가치가 있는 것 (A등급 — 버그·운영 위험)

### A1. backfill_period.py가 브라우저 안전판(MAX_BROWSERS/LazyPage)을 우회함

`backfill_period.py:124`는 워커마다 `new_context_page(pw)`를 직접 호출합니다. 브라우저 동시 개수 제한(`_browser_permits`)과 지연 생성은 전부 `LazyPage`(browser.py:203~) 안에만 있으므로, 이 스크립트는 **워커 수 = 크롬 프로세스 수**로 돌아갑니다. 지금 일일 퀘스트에서 `BACKFILL_PERIOD_CONCURRENCY=50`으로 실행하고 계시니, 대상 건수가 50건을 넘는 날엔 크롬 50개가 동시에 뜹니다 — 2026-07-30에 시스템을 먹통으로 만들었던 바로 그 패턴이 이 스크립트에만 남아 있습니다. rescan_inprogress.py는 2026-08-04에 LazyPage로 고쳐졌는데 backfill_period.py만 누락된, 전형적인 "복사된 코드에 수정이 한쪽만 반영된" 사례입니다. 수정은 rescan과 동일하게 LazyPage로 교체하면 되고 판정 결과에는 영향이 없습니다.

### A2. requirements.txt에 beautifulsoup4/lxml 누락

`httpfetch.py:21`이 `from bs4 import BeautifulSoup`을, `httpfetch.py:181`이 `lxml` 파서를 쓰는데 requirements.txt에는 둘 다 없습니다. 지금 로컬에는 우연히 설치되어 있어 돌아가지만, 새 환경(다른 개발자, 새 맥, 서버 이전)에서는 resolve_links 전체가 import 단계에서 죽습니다. `beautifulsoup4`, `lxml` 두 줄 추가로 끝나는 수정입니다. 같은 맥락에서 버전 고정(`==` 또는 `>=` 하한)도 없어서 재현 가능한 설치가 안 됩니다 — pycache에 cpython-39와 cpython-312가 섞여 있는 것도(scripts/__pycache__/) 인터프리터 두 개가 번갈아 쓰이고 있다는 신호라, 파이썬 버전도 하나로 못박는 게 좋습니다.

### A3. load.py의 입력 폴더 선택이 조용히 데이터를 누락시킬 수 있음

`load.py:14`는 import 시점에 "04_resolved에 jsonl이 하나라도 있으면 그걸, 없으면 03_load_ready를" 입력으로 고릅니다. 04_resolved는 resolve_links가 돌 때마다 그 시점의 전체로 재조립되므로, **transform은 돌았는데 resolve를 건너뛴 경우**(예: `run_pipeline.py --skip-resolve`, 또는 resolve가 중간에 죽은 날) 04_resolved에는 새 포스트가 없고, load.py는 04만 읽으므로 그 새 포스트들이 **에러도 없이 조용히 적재 대상에서 빠집니다**. load는 UPDATE를 안 하니 나중에 resolve를 돌리고 load를 다시 실행하면 복구는 되지만, "빠졌다는 사실"을 알려주는 장치가 없습니다. 최소한 03과 04의 키 집합을 비교해서 04에 없는 건수를 경고로 출력하거나, 04에 없는 항목은 03 원본으로 보충하는 병합 로직이 필요합니다.

### A4. load.py의 존재확인→INSERT 경합은 INSERT IGNORE로 구조적으로 제거 가능

동시 실행 시 `Duplicate entry` 에러가 나는 문제(README에도 문서화됨)는 post_id/video_id에 이미 UNIQUE 제약이 있으므로 `INSERT IGNORE`(또는 `ON DUPLICATE KEY UPDATE id=id`)로 바꾸면 SELECT 왕복도 없어지고 경합 자체가 사라집니다. "이미 있으면 건너뛴다"는 의미가 완전히 동일해서 결과 보존 전제에 부합하고, 부수 효과로 적재 속도도 절반 왕복만큼 빨라집니다(D2와 연결).

### A5. 일일 퀘스트의 `2> /tmp/resolve_noise.log` 리다이렉트가 진짜 에러도 삼킴

stderr를 통째로 /tmp로 보내면 Playwright 노이즈와 함께 진짜 예외/경고(예: DEEPSEEK_KEY 누락 메시지도 stderr)도 안 보이게 됩니다. 노이즈의 근원을 코드에서 억제(로깅 레벨 도입, B6 참고)하고 리다이렉트는 없애는 방향을 권합니다.

---

## 3. 구조/유지보수 (B등급 — 대공사의 본체)

### B1. "스크립트 무더기"를 진짜 패키지로 — 실행 방식 통일

지금은 scripts/가 패키지가 아니라서 `from common import ...`가 "실행 디렉터리가 scripts/이거나 sys.path에 우연히 잡힐 때"만 동작하고, 그 결과 실행 규약이 두 가지로 갈라져 있습니다(대부분은 저장소 루트에서 `python3 scripts/x.py`, resolve_links만 `cd scripts && python3 -m resolve_links`). 일일 퀘스트 명령에 `cd scripts && ... && cd ..`가 끼어 있는 것 자체가 이 갈라짐의 증상입니다.

제안: 저장소 루트에 `pyproject.toml`을 두고 코드를 `gonggu/` 패키지로 옮깁니다(`gonggu/common.py`, `gonggu/stages/classify.py`, `gonggu/resolve_links/`, `gonggu/linkbio_parser/`...). `pip install -e .` 한 번이면 어느 디렉터리에서든 `python3 -m gonggu.classify`로 실행되고, 콘솔 엔트리포인트(`gonggu-classify`, `gonggu-daily` 등)도 공짜로 생깁니다. 파일 이동은 git이 rename으로 추적하니 히스토리도 보존됩니다. 판정 로직은 한 줄도 안 바뀌는 순수 구조 변경입니다.

### B2. LLM 배치 러너 공통화 — 같은 코드가 3벌

classify.py, classify_yt_ppl.py, classify_category.py는 구조가 사실상 동일합니다: 429 전용 장기 재시도 + 일반 재시도 루프(`classify.py:38-63` ≈ `classify_yt_ppl.py:45-70` ≈ `classify_category.py:61-80`), 체크포인트에서 done 집합 계산 → todo 산출 → LIMIT, ThreadPoolExecutor + lock + REPORT_EVERY=30 진행 로그 + 실패 샘플 3개 출력(`classify.py:98-127` ≈ `classify_yt_ppl.py:96-128` ≈ `classify_category.py:162-192`). 세 파일 합쳐 약 500줄 중 350줄 이상이 복제입니다.

이걸 `llm_batch.py` 하나로 추출하면 각 분류 스크립트는 "키 함수 + 프롬프트 빌더 + 입출력 경로"만 선언하는 30~50줄짜리가 됩니다. A1과 같은 "한쪽만 고쳐지는" 사고의 원천이 사라지고, 429 정책이나 타임아웃을 바꿀 때 한 곳만 만지면 됩니다. 프롬프트와 호출 순서, 체크포인트 판정 기준(`classification && !classification_error`)을 그대로 옮기면 결과는 바이트 단위로 동일합니다. 참고로 그 done 판정 기준 자체도 지금 classify.py, classify_yt_ppl.py, run_pipeline.py:63 세 곳에 복제되어 있습니다.

### B3. 크롤링 워커 풀 공통화 — 같은 코드가 4벌

queue + threading.Thread + lock + counters + `release_if_contended()` + ITEM_DELAY 패턴이 runner.py(`_resolve_worker`), rescan_inprogress.py(`_worker`), backfill_period.py(`_worker`), _diag_sample.py(`_diag_worker`)에 각각 손으로 구현되어 있고, 미묘하게 서로 다릅니다(바로 그 "미묘한 차이"가 A1 버그입니다). `crawl_pool.py` 하나로 추출해서 "아이템 처리 함수 + 결과 저장 함수"만 주입받게 하면, 브라우저 수명 관리·허가증·경고 출력·통계가 한 곳에서 관리됩니다. 워커 수 경고 로직(`n_workers > MAX_BROWSERS * 3`)도 지금 runner와 rescan 두 곳에 복제되어 있습니다.

### B4. 플랫폼(ig/yt) 이중화를 메타테이블로 접기

post/video 쌍으로 된 SQL과 분기가 저장소 전체에 퍼져 있습니다: load.py의 INSERT/CHECK 4쌍, rescan의 SELECT_POST/SELECT_VIDEO + UPDATE_POST/UPDATE_VIDEO, backfill의 SELECT/UPDATE 쌍, update_gonggu_stage의 TABLES, 그리고 `publish_date vs publishDate` 분기(common.py:203-214 등). DB 컬럼명이 원본 hifen DB를 따라가는 설계(README에 근거 명시)는 그대로 두되, **파이썬 쪽에서만** 플랫폼 메타테이블 하나로 접을 수 있습니다:

```python
PLATFORMS = {
  'ig': Platform(parent_table='gonggu_post', product_table='gonggu_post_product',
                 id_col='post_id', date_field='publish_date', ...),
  'yt': Platform(parent_table='gonggu_video', product_table='gonggu_video_product',
                 id_col='video_id', date_field='publishDate', ...),
}
```

SQL은 이 메타에서 생성(테이블/컬럼명은 코드 상수라 인젝션 위험 없음)하면 같은 쿼리를 두 번 쓸 일이 없어지고, "포스트 쪽만 고치고 비디오 쪽을 깜빡"하는 부류의 사고가 구조적으로 불가능해집니다. 생성된 SQL이 기존 문자열과 동일한지는 테스트로 못박습니다(B5).

### B5. 테스트가 0개 — 대공사의 선행 조건

지금 저장소에는 테스트가 하나도 없습니다. "결과 완전 보존" 리팩터링은 테스트 없이는 말로만 보장하는 것이 됩니다. 다행히 이 코드는 테스트하기 좋은 순수 함수가 많습니다: `transform_one`/`_compute_stage`/`_valid_date`(transform.py), `rank_candidates`/`_dedup_key`/`handle_matches`(ranking.py), `normalize_url`/`_filter_link_pairs`(links.py), `extract_jsonld_blocks`/`_snippet`/`_strip_hidden`(httpfetch.py), `looks_discontinued`/`recover_from_block`(antibot.py), `extract_balanced_json`(linkbio_parser/extract.py), `_key`/`product_key`, `load_jsonl`의 last-wins 규칙 등.

두 층으로 제안합니다. 첫째, 위 순수 함수들의 단위 테스트(주석에 기록된 실측 사고 사례들 — kkang_twins_ 언더바, "https:///api/r/" 깨진 URL, hi.thehyundai.com/error, display:none 모달 — 을 그대로 테스트 케이스로 박제). 둘째, **골든 파이프라인 테스트**: 현재 02_classified에서 대표 샘플 수백 건을 뽑아 고정 입력으로 저장하고, transform(오늘 날짜만 고정하면 완전 결정론)을 돌려 나온 03_load_ready 출력을 골든 파일로 커밋합니다. 이후 모든 리팩터링 커밋에서 이 골든 diff가 비면 통과입니다. LLM/크롤링 단계는 체크포인트가 있어 재실행 자체가 없으므로, 골든 검증은 "체크포인트를 읽어 산출물을 조립하는 코드"(runner.build_resolved_file 등)에 적용하면 됩니다.

### B6. 일일 퀘스트를 명령 1개로 — 오케스트레이터 완성

run_pipeline.py는 1~5번만 알고, 실제 일일 퀘스트는 6→1→8-1→2→8-2→3→4→5→7→9 순서의 명령 10개를 수동으로 치는 구조입니다. 순서 제약(6이 7·9보다 먼저, 4가 5보다 먼저)이 사람 머릿속에만 있으니, `gonggu-daily` 하나로 전체 순서를 코드화하고 각 단계의 요약 출력을 `data/logs/2026-08-05.log` 같은 실행 로그로 남기는 걸 권합니다. 여기에 마지막 단계로 "오늘의 요약"(단계별 처리/실패 건수, 패스트패스 적중률, LLM 토큰 사용량 — llm_usage_report.py 재사용)을 붙이면 매일 아침 로그 한 파일만 보면 되는 운영이 됩니다. 동시 실행 방지 lockfile(classify/resolve/load의 "두 번 띄우지 말 것" 제약을 코드로 강제)도 이때 함께 넣으면 좋습니다.

### B7. 그 외 유지보수 관찰

`resolve_links/__init__.py:51-53`의 docstring이 옛 파일명(link_resolution.json, load_ready_resolved.json)을 언급하는 등 문서와 실체의 어긋남이 몇 군데 있고, README의 다이어그램 파일(docs/pipeline_diagram.html)도 "최신 아님"으로 표기된 채입니다 — 대공사 마지막에 문서 싱크를 한 번 맞춰야 합니다. category 계열(build_category_dataset/classify_category/export_unclassified/category_dashboard)은 본줄기와 단절된 채 ~/Desktop 경로(classify_category.py:41-42)를 기본값으로 쓰는 실험 단계인데, 후반부 50%에 카테고리를 DB로 통합할 계획이라면 이 계열의 입출력을 data/ 밑으로 옮기고 파이프라인에 편입하는 설계가 필요합니다(어느 테이블에 저장할지 스키마 결정 포함).

---

## 4. 데이터 관리 (C등급)

### C1. "매번 전체 재계산·전체 스캔" 구조의 성장 문제

현재 data/의 실측: 02_classified 약 145MB(하루 5~12MB씩 순증), 01_raw+01_raw_yt_ppl 약 90MB, link_resolution.jsonl 23.6MB, llm_usage.jsonl 3.3MB — 전부 무한 성장입니다. 그런데 classify.py는 매 실행마다 01_raw **전체**와 02_classified **전체**를 메모리에 올려 todo를 계산하고(common.load_json_dir), transform.py는 02_classified 전체를 재계산하고, runner.build_resolved_file은 03 전체 + 체크포인트 전체를 다시 조립합니다. 지금은 실행당 수십 초 수준이지만 한두 달 뒤엔 분 단위가 되고, 메모리도 수 GB로 올라갑니다.

핵심 관찰: load.py가 UPDATE를 안 하므로 **이미 DB에 적재된 날짜를 다시 transform/조립하는 건 결과에 아무 영향이 없는 순수 낭비**입니다(보강 단계 6·7·9는 전부 DB에서 직접 읽지, 파일을 안 봅니다). 따라서 결과 보존을 깨지 않고 이렇게 바꿀 수 있습니다: 기본 실행은 "최근 N일(예: DAYS_BACK+여유) 날짜 파일만" 읽어 재계산하고, 필터 규칙을 바꿔 전체를 다시 계산하고 싶을 때만 `--full` 플래그로 예전 동작을 유지합니다. 같은 원리로 classify의 done 집합도 전체 스캔 대신 날짜별 키 인덱스(또는 최근 N일 한정)로 계산할 수 있습니다.

여기에 **아카이브 정책**을 더합니다: 적재 완료 + 종료 상태가 확정된 날짜의 01/02 파일은 `data/archive/`(또는 gzip)로 이동. JSONL이라 gzip 비율이 좋고(대략 1/5), 필요하면 언제든 풀어서 --full 재계산에 쓸 수 있습니다. `data/raw/`(빈 레거시 폴더)와 `_migrated_backup/`(약 130MB, 7월 마이그레이션 이전 백업)은 외장/클라우드로 옮기고 저장소 밖으로 빼는 걸 권합니다.

### C2. append-only 체크포인트에 컴팩션 추가

link_resolution.jsonl은 "마지막 줄이 이긴다" 규약의 append-only인데, rescan이 매일 재탐색 결과를 append하므로 같은 key의 줄이 계속 쌓입니다(현재 23.6MB — 로드 시 매번 전체 파싱). 규약이 명확하므로 컴팩션은 안전합니다: "전체를 읽어 last-wins로 접은 뒤 임시파일에 쓰고 os.replace" 하는 `gonggu-compact`를 만들어 주간 1회(또는 daily 오케스트레이터 말미에 파일이 임계 크기를 넘으면) 실행. period_backfill.jsonl, llm_usage.jsonl(이건 날짜별 분할이 더 적합)도 같은 처리 대상입니다.

### C3. 02_classified가 원본 전체를 복제 저장

classified 레코드는 `{**post, 'classification': ...}` 형태라 01_raw의 원문 캡션 전체가 02에 그대로 한 번 더 저장됩니다(용량 2배의 주범 — 02가 01보다 큰 이유). 결과 보존 전제에서 지금 당장 바꿀 필요는 없지만, 후반부 설계에서 "02에는 키 + classification만 저장하고 transform이 01과 조인"하는 구조로 가면 저장량이 절반 이하로 줄고 C1의 스캔 비용도 같이 줄어듭니다. 마이그레이션이 필요한 변경이라 3단계(로드맵 참조)에 배치했습니다.

### C4. 파일과 DB의 이중 진실은 "의도된 설계"로 명문화

rescan이 DB와 link_resolution.jsonl 양쪽에 쓰는 것(rescan_inprogress.py:23-25)은 04_resolved 재조립 시 재탐색 결과가 잊히지 않게 하는 의도된 동기화인데, 이런 "어디가 진실의 원천인가" 규칙이 코드 주석에 흩어져 있습니다. 대공사 문서에 명시적으로 박아두는 걸 권합니다: **1~4단계의 진실은 파일(+체크포인트), 5단계 이후의 진실은 DB, link_resolution.jsonl은 둘을 잇는 유일한 다리.** 후반부 50%에서 다운스트림 개발자와의 인터페이스가 DB로 확정되어 있으므로, 장기적으로는 체크포인트를 DB 테이블로 옮겨 이중성을 없애는 선택지도 있지만(운영 단순화), 로컬 파일의 grep 가능한 디버깅 편의가 실제로 활용되고 있어 지금 단계에서는 권하지 않습니다.

---

## 5. 처리 속도 (D등급)

전체 소요 시간의 지배 항은 명확히 resolve_links(크롤링)이고, 코드 주석의 실측(워커 가용시간의 약 34%가 대기)과 일치하는 구조적 여지가 남아 있습니다.

### D1. resolve_links의 대기 구간 — ITEM_DELAY의 전역 적용

`ITEM_DELAY=3`초가 **모든** 상품 사이에 적용되는데(runner.py:100), 패스트패스(requests)로 끝난 건이나 linkbio 캐시 적중 건은 외부 사이트에 부하를 준 적이 없으므로 3초를 쉴 안티봇상 이유가 없습니다. "이번 아이템에서 실제로 브라우저/외부 요청을 했을 때만 delay"로 바꾸면 판정 결과에 영향 없이 패스트패스 비중만큼 처리량이 올라갑니다(적중률이 로그에 찍히고 있으니 효과도 즉시 측정 가능). 더 나아가면 도메인별 쿨다운(마지막 접근 시각 기반)이 정석이지만, 이는 2단계 이후 옵션으로.

### D2. load.py의 건별 SELECT+INSERT+commit

현재 항목당 왕복이 3~4회(존재확인, 부모 INSERT, 상품 N개 INSERT, commit)입니다. A4의 INSERT IGNORE로 존재확인 왕복을 없애고, 커밋을 소배치(예: 50건 단위, 실패 시 그 배치만 건별 재시도로 폴백)로 묶으면 "한 건 실패가 다른 건을 막지 않는다"는 현재 보장을 유지하면서 적재 시간이 크게 줄어듭니다. 다만 load는 하루 수백~수천 건 규모라 절대 시간은 작으니 우선순위는 낮습니다.

### D3. LLM 꼬리 지연

config.py:55-63의 실측 기록대로 이 단계는 평균이 아니라 꼬리(60~117초 호출)가 전체 시간을 정합니다. call_llm의 timeout이 120초로 전역 고정인데, resolve 경로(LLM#2/#3)만 "짧은 타임아웃(예: 45초) + 1회 재시도"로 바꾸면 꼬리가 잘립니다. 단, 재시도는 LLM 비결정성 때문에 답이 달라질 수 있어 엄밀한 의미의 결과 보존과 상충합니다 — 이건 결과가 바뀔 수 있음을 인지하고 켜는 **옵트인 환경변수**로 넣는 걸 권합니다(어차피 지금도 타임아웃 → error → rescan 재시도로 비결정적이긴 합니다).

### D4. 소소한 것들

update_gonggu_stage.py는 전 행을 파이썬으로 가져와 건별 UPDATE하는데, 전이 규칙이 날짜 비교뿐이므로 set 기반 UPDATE 2~3문으로 대체 가능합니다(현재 행수에선 체감 없음 — 순수 위생). transform의 clear+전체 재작성은 C1 증분화와 함께 자연히 해소됩니다. classify의 CONCURRENCY=200은 이미 검증된 값이니 유지.

---

## 6. 대공사 로드맵 (제안)

각 단계는 독립적으로 완결되며, 단계 사이에 일일 퀘스트를 재개할 수 있습니다. 모든 단계의 완료 조건에 "골든 diff 통과"가 포함됩니다.

**0단계 — 안전망 (반나절).** git 태그로 현 상태 박제, data/ 스냅샷 백업, pytest 도입 + B5의 단위 테스트(순수 함수) + 골든 파이프라인 테스트 작성. 파이썬 버전 하나로 고정, requirements 보강(A2 포함, 버전 고정).

**1단계 — 즉효 수정 (반나절).** A1(backfill LazyPage), A3(load 입력 폴더 경고/병합), A4(INSERT IGNORE), A5(stderr 리다이렉트 제거를 위한 로깅 정리 최소분). 이 단계만 반영해도 일일 퀘스트의 안정성이 눈에 띄게 올라갑니다.

**2단계 — 구조 대공사 (2~3일, 이 기간 퀘스트 중단 권장).** B1 패키지화 → B2 LLM 배치 러너 추출 → B3 크롤링 워커 풀 추출 → B4 플랫폼 메타테이블 → B6 오케스트레이터(gonggu-daily + 실행 로그 + lockfile). 매 추출마다 골든 diff + 소량 라이브 실행(LIMIT=20)으로 검증. 기존 데이터 파일/체크포인트/DB는 형식 그대로라 마이그레이션 없음.

**3단계 — 데이터 관리 (1~2일).** C1 증분 transform(--full 보존) + 아카이브 정책, C2 컴팩션 도구, _migrated_backup 정리, (선택) C3 02_classified 슬림화 설계. llm_usage 날짜 분할.

**4단계 — 속도 (1일).** D1 조건부 ITEM_DELAY, D2 load 배치화, (옵트인) D3 꼬리 타임아웃. 각 항목은 전후 처리량을 로그 수치로 비교.

**5단계 — 후반부 준비 (설계 세션).** category 계열의 파이프라인 편입(스키마 포함), 문서 싱크(B7), 다운스트림 개발자용 인터페이스 문서(테이블 계약 + link_status 의미론) 확정.

---

## 7. 결과 동일성 검증 방법 (요약)

transform은 오늘 날짜만 고정하면(예: `GONGGU_TODAY` 환경변수 또는 테스트에서 date 주입) 완전 결정론이므로, 리팩터링 전후로 02_classified 전체를 입력 삼아 03_load_ready를 두 벌 생성해 diff가 비는지 확인합니다. classify/resolve는 체크포인트 덕분에 리팩터링 전후로 재실행이 일어나지 않으므로 "체크포인트 → 산출물 조립" 경로만 같은 방식으로 diff합니다. DB 적재는 리팩터링 후 첫 실행에서 "삽입 0건 / 스킵 전건"이 나오는지로 검증합니다(이미 다 들어가 있어야 정상). 이 세 가지 확인이 로드맵 각 단계의 게이트입니다.
