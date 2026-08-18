# 데일리 파이프라인 재점검 — 2026-08-18

목적: `python3 -m gonggu.daily`의 각 단계를 순서대로(1→10) 깊게 점검해서 문제점과 해결 방향을
기록한다. 기존 코드 주석/메모리의 시행착오 서술은 참고만 하고, 지금 상태를 기준으로 새로
판단한다. resolve_links는 가장 의심되는 단계라 별도로 깊게 본다(아래 "4. resolve_links" 절,
현재 점검 전).

---

## 1~3단계 점검 (update_gonggu_stage / fetch_source / fetch_yt_ppl / classify / classify_yt_ppl / transform)

(옛 문제 1 — classify.py/classify_yt_ppl.py가 매일 CLASSIFIED_DIR(실측 223MB) 전체를 각자
재로드하던 문제는 **해결 완료**. `common.py`에 두 스크립트가 공유하는 작은 done-키 인덱스
(`CLASSIFY_DONE_KEYS_FILE`, `load_classify_done_keys`/`record_classify_done_key`)를
추가했다 — 최초 1회만 CLASSIFIED_DIR 전체를 훑어 부트스트랩하고, 이후로는 이 인덱스 파일만
읽는다. 실측(2026-08-18, 실제 데이터로 검증): 부트스트랩 1회 2.46초 → 이후 매 호출 0.27초로
9배 단축(인덱스 파일 크기 2.6MB, 원본 223MB의 1/85). 두 스크립트가 다루는 key 공간이 fetch
단계 SQL에서부터 배타적이라 인덱스 공유가 안전함을 확인. 이걸로 문제 9(두 스크립트의 로드
공유)도 같이 해결됐다. **다만 RAW_DIR/RAW_DIR_YT_PPL(01_raw 82M, 01_raw_yt_ppl 68M)은 여전히
매일 전체 로드한다** — 분류에 실제 캡션 내용이 필요해서 이건 key만으로 대체할 수 없다. 계속
자라는 값이라 언젠가 다시 볼 만하지만, 지금 당장의 "이중 재파싱" 문제보다는 훨씬 작고 급하지
않다고 판단해 이번엔 손대지 않았다. `common.load_classify_done_keys` 등에 대한 신규 테스트
3개 추가(`tests/test_common_io.py`), 전체 테스트(387개) 통과 확인.)

(옛 문제 2 — classify.py의 dedup이 캡션 수정 후에도 재분류하지 않던 것은 **사용자 확인 결과
문제 아님으로 종결**. 실제로 게시 후 캡션이 수정되는 경우가 없어서 재검토 로직이 필요 없다고
확인함(2026-08-18). classify.py 상단 docstring에 이 정책이 의도된 것임을 명시해뒀다.)

### 문제없음으로 확인

- **fetch_source.py의 IG_QUERY 캡션 중복 우려(2026-08-18 실DB 확인으로 기각)**:
  `instagram_post_description.post_id`가 **PRIMARY KEY**(`SHOW INDEX`로 확인)라 post_id당
  캡션 행이 구조적으로 하나뿐이다 — GROUP BY에 `d.description`이 섞여 있어도 중복 raw
  레코드가 생길 여지 자체가 없다. 옛 문제 3, 이 확인으로 완전히 해소.
- **update_gonggu_stage.py**: 강제종료 조건(`stage_with_forced_end`)이 정확히 "시작일 있음 +
  종료일 없음 + 진행중" 조합에만 걸리고, 종료일이 명시된 공구는 건드리지 않음. idempotent.
- **transform.py**: 증분 모드의 서명(mtime+size) 기반 변경 감지가 견고하고, `--full` 경로와
  결과가 갈릴 여지가 없음. `_compute_stage`를 update_gonggu_stage/backfill_period가 공유해서
  적재 시점 계산과 어긋나지 않음.

---

## 4. resolve_links — 깊은 점검

패키지 전체(config/core/browser/httpfetch/picker/runner/antibot/llm/redirect/links/ranking/
matching/youtube/reverify_uc, 총 2300줄)를 다 읽고 실제 daily.py 설정값과 대조했다. 지금
"resolve_links가 가장 잘못된 것 같다"는 감이 맞았다.

(옛 문제 4 — CRAWL_RECYCLE_SEC가 브라우저를 하나도 안 쓰는 Tier0에도 걸려서 의미 없는 전체
프로세스 재시작을 유발하고 Tier1 진입을 지연시키던 문제는 **해결 완료**. `crawl_pool.py`의
`_watchdog`이 `use_playwright=False`면 재기동(`CRAWL_RECYCLE_SEC`)을 무시하도록 고쳤고
(`effective_recycle = RECYCLE_AFTER_SEC if use_playwright else 0.0`), STALL_TIMEOUT(진짜
먹통 감지)은 그대로 유지했다. 재발 방지 테스트
`tests/test_crawl_pool.py::TestNoPlaywrightMode::test_recycle_ignored_without_playwright`
추가, 전체 테스트(371개) 통과 확인.)

(옛 문제 5 — RESOLVE_SHARD_COUNT 샤딩이 Tier0 동시성/도메인 상한을 재분배하지 않던 문제는
**부분 해결**. `daily._run_resolve_links_sharded`가 이제 `RESOLVE_FAST_CONCURRENCY`와
`MAX_PER_DOMAIN`도 `MAX_BROWSERS`/`RESOLVE_CONCURRENCY`와 똑같이 `_split_evenly`로 샤드 수만큼
나눠 배분한다 — 총 동시 네트워크 요청 수와 도메인당 실질 동시 접근이 샤드 수배로 뛰는 가장 큰
위험은 없앴다. 재발 방지 테스트 `tests/test_resolve_sharding.py`에 2개 추가. **다만
`HOST_COOLDOWN_SEC`(호스트별 차단 후 쿨다운)는 여전히 각 샤드 프로세스 메모리 안에서만
관리되어 샤드 간에 공유되지 않는다** — 한 샤드가 어떤 호스트의 차단을 감지해도 다른 샤드는
모른 채 계속 요청을 보낼 수 있다는 한계가 남아있고, 이건 프로세스 간 공유 저장소가 필요한
더 큰 작업이라 지금은 손대지 않았다. 샤딩을 실제로 켤 계획이 생기면 이 한계를 다시 볼 것.)

### 부가 — 샤딩은 무거운 디렉터리 재로드 비용도 샤드 수만큼 중복시킴

문제 1(classify.py 이중 재로드)과 같은 종류: 샤드 N개면 `LOAD_READY_DIR`/`RESOLUTION_FILE`을
N개 프로세스가 각자 전체 로드한다. 지금은 프로세스 분리가 목적(격리)이라 어느 정도는 불가피하지만,
샤드 수를 올릴수록 이 고정비도 같이 커진다는 걸 인지하고 있을 것.

### 문제없음으로 확인 (resolve_links)

- **httpfetch.py의 requests/브라우저 파싱 일치성**: `extract_jsonld_blocks`/`_strip_hidden`/
  `_snippet`을 requests·uc 경로가 공유하도록 설계되어 있어 "엔진 바꾸면 판정이 뒤집히는" 위험이
  구조적으로 낮음.
- **picker.py의 확신도별 분기**(high/medium/prefetched product vs link)는 과거 오탐 사고들과
  정확히 대응되고 서로 모순되지 않음.
- **domain_gate/MAX_PER_DOMAIN이 "실제 목적지 도메인"에 거는 설계**(runner.py 상단 docstring)는
  단일 프로세스 안에서는 여전히 유효하고 잘 근거가 있음(옛 문제 5가 이게 "단일 프로세스 안에서만"
  이라는 전제가 샤딩으로 깨지는 문제였는데, MAX_PER_DOMAIN을 샤드별로 나누는 것으로 부분 해결함
  — 위 참고).

---

## 5. load / rescan_inprogress / backfill_period_inpock / backfill_period / maintenance 점검

(옛 문제 6 — rescan_inprogress.py의 link_status='error'가 백오프/은퇴 없이 매일 무기한
재시도되던 문제는 **해결 완료**. `classify_target()`에 `RESCAN_ERROR_MAX_ATTEMPTS`(기본 14)
상한을 추가해서, 그만큼 계속 error로만 끝난 상품은 은퇴시킨다 — unresolved/hold의 백오프
소진 은퇴와 대칭. 재발 방지 테스트 2개 추가(`test_error_retires_after_max_attempts` 등),
전체 테스트 통과 확인.)

(옛 문제 7 — backfill_period_inpock.py의 not_found가 영구 스킵이라 backfill_period.py와
정책이 불일치하던 문제는 **문제 10(병합)로 해결**. 두 스크립트를 하나로 합치면서 체크포인트/
재시도 정책도 하나(`_should_skip`, 쿨다운+MAX_ATTEMPTS)로 통일했다 — 아래 문제 10 참고.)

### load.py — 문제없음, 지난 감사(A3/A4) 수정이 견고하게 자리잡음

- `split_unresolved`가 03/04 키 집합을 비교해 04에 없는 신규 항목을 명시적으로 경고하고
  보류(`LOAD_UNRESOLVED=1`로만 강제 포함)하는 구조라 예전 A3("조용히 누락") 문제는 해결된 채로
  유지되고 있다.
- 중복 INSERT 경합(A4)도 `_is_duplicate_entry`로 errno 1062만 정확히 골라 스킵 처리 — `INSERT
  IGNORE`를 안 쓴 이유(컬럼 길이 초과 등 다른 에러까지 삼키면 안 됨)도 타당하다.
- **문제 3(fetch_source 캡션 1:N 중복 raw) 관련 추가 확인**: 만약 실제로 중복 raw가 발생해도,
  `load_item`의 "SELECT 존재확인 → INSERT"가 **같은 트랜잭션/커넥션 안에서는 자기 트랜잭션의
  아직 커밋 안 된 INSERT까지 그대로 보이므로(같은 배치 안이든, 이미 커밋된 이전 배치든)** 중복
  두 건이 같은 `load_all` 실행에 들어와도 실제로 DB에 두 번 꽂히는 일은 없다 — 앞서 "Duplicate
  entry 에러가 날 수 있다"고 적은 부분을 정정한다. 남는 비용은 여전히 classify.py의 이중 LLM
  호출뿐이다(문제 3은 그대로 유효, 다만 데이터 정합성 리스크는 없음).

### backfill_period.py — 문제없음

지난 감사(A1)의 LazyPage 우회 문제가 `crawl_pool.py` 공용 배관 채택으로 완전히 해결됐고,
체크포인트(found 영구/not_found 쿨다운+상한)도 backfill_period_inpock보다 오히려 더 안전한
설계다. "판단불가 + link_status=done"만 대상으로 삼아 기존 값을 덮어쓸 여지가 구조적으로 없다는
점도 코드와 일치한다.

### maintenance.py — 문제 1과 연결되는 점만 재확인, 그 외 문제없음

컴팩션(last-wins 접기)·usage 로테이션 로직 자체는 안전하고 멱등이다. `ARCHIVE_AFTER_DAYS`가
daily.py STAGES에서 기본 미지정이라는 점은 이미 문제 1에서 다뤘다 — 이 스크립트 자체의 결함이
아니라 "daily가 이 옵션을 켜주지 않는다"는 호출부 설정의 문제.

---

## 6. 단계 간 병합/분리가 필요한 지점

(옛 문제 8 — resolve_links.finalize()의 _dump_linkbio가 샤딩(--finalize)과 함께 쓰면 조용히
0건이 되던 버그는 **해결 완료**. `links.py`에 파싱 결과의 영구 저장소
(`LINKBIO_HUB_CACHE_FILE`, append-only jsonl, 다른 체크포인트와 동일 패턴)를 추가해서
`linkbio_candidates()`가 파싱에 성공하는 즉시 프로세스 메모리(`_linkbio_cache`)뿐 아니라
이 파일에도 남기게 했다. `_dump_linkbio`는 이제 프로세스 로컬 캐시 대신
`load_persisted_linkbio_data()`로 이 파일을 읽으므로, `finalize()`를 부르는 프로세스가
실제 파싱이 일어난 프로세스와 달라도(샤딩의 `--finalize`) 정상 동작한다 — `finalize()`
자체의 구조는 안 바꿨다(재구성보다 데이터 소스를 durable하게 바꾸는 쪽이 더 단순했음).
기존 3개 테스트를 이 새 경계에 맞게 고치고, 회귀 테스트
`tests/test_resolve_linkbio_dump.py::test_linkbio_candidates_persists_across_process_boundary`
추가. 전체 테스트(374개) 통과 확인.)

(옛 문제 9 — classify.py + classify_yt_ppl.py의 CLASSIFIED_DIR 로드 공유는 옛 문제 1을
고치면서 **함께 해결됨** — 자세한 내용은 위 "1~3단계 점검" 절의 옛 문제 1 항목 참고. 두
스크립트는 판정 로직/프롬프트는 그대로 분리 유지한 채, 공유 done-키 인덱스만 함께 쓴다.)

(옛 문제 10 — backfill_period_inpock.py + backfill_period.py 병합은 **해결 완료**.
`backfill_period.py` 하나로 합쳤고, resolve_links의 Tier0/Tier1과 같은 패턴으로
`crawl_pool.run_crawl_pool(use_playwright=False)`를 이용해 Tier0(인포크 텍스트, 브라우저
없음)를 먼저 돌고 못 찾은 것만 Tier1(몰 크롤)로 넘긴다. 체크포인트/재시도 정책도
`data/output/period_backfill.jsonl` 하나(쿨다운+MAX_ATTEMPTS)로 통일해서 문제 7도 같이
해결됐다. `backfill_period_inpock.py` 파일은 삭제하고 `daily.py` STAGES에서 옛 9-0 항목도
제거, README·테스트(`tests/test_backfill_period.py`로 이름 변경 + `_should_skip`/
`_has_any_source` 신규 테스트 추가) 갱신. 전체 테스트(384개) 통과 확인.

참고로 두 티어 병합은 "인포크 텍스트가 애초에 안 바뀌면 재시도해도 같은 결과"인 낭비를
완전히 없애진 못한다(텍스트 해시 비교 같은 더 정교한 방법은 안 함) — 지금은 문제 7이 지적한
"영구 실패" 쪽이 훨씬 심각해서 그것부터 없앴고, 이 낭비는 남아있는 사소한 트레이드오프다.)

### 병합해선 안 되는 곳(표면적으론 비슷해 보이지만)

- **fetch_source ↔ fetch_yt_ppl / classify ↔ classify_yt_ppl의 판정 로직 자체**: "이미 검증된
  LLM#1에 전혀 영향 안 주기 위해 완전히 분리"라는 의도적 설계(README 명시) — 위 문제 9는 로딩
  비용 공유일 뿐, 판정 로직/프롬프트는 분리 유지해야 함.
- **classify ↔ transform**: 이 분리 덕분에 transform의 증분 모드(서명 기반)가 가능하다. 합치면
  그 최적화가 무너진다.
- **resolve_links ↔ load**: load는 반드시 resolve가 끝난 뒤 실행돼야 하는 순서 의존이라 분리가 맞다.

### 느슨한 후보(확신도 낮음, 참고용) — Tier0 fast-path를 rescan_inprogress도 쓸 수 있게 추출

Tier0(브라우저 없는 빠른 패스)가 `runner.py`에만 있고 `rescan_inprogress.py`는 옛 방식(항상
Playwright 워커)이다. `RESCAN_CONCURRENCY`가 10으로 낮은 이유가 "40으로 올리면 브라우저
churn으로 얼어붙는다"였는데, Tier0를 rescan에도 적용하면 브라우저가 필요 없는 재탐색 건이
브라우저를 안 뺏어가서 이 상한을 다시 올릴 여지가 생길 수 있다 — 실측 없는 추정이라 확신도는
낮음. 이 저장소가 반복 경험한 "같은 로직이 여러 곳에 복제되고 한쪽만 고쳐진다"는 패턴(A1과
동일 계열)이라, Tier0를 crawl_pool.py 레벨의 재사용 가능한 옵션으로 뽑아두는 걸 고려할 만함.

---

## 7. LLM#1~#3 점검 (모델 변경/비용 고려 제외 — 프롬프트 로직·처리 일관성·데이터 공백 관점)

(옛 문제 11 — LLM#1의 url_type이 실제로는 안 쓰이는 장식 필드였고, resolve_links는 완전히
별도의 코드 도메인 목록(`MALL_DOMAINS` 등)으로 독자 재판단하던 문제. **일부 해결** — url_type
필드 자체를 없애거나(다운스트림 개발자가 이 컬럼을 참고용으로 쓰는 계약이라 스키마를 건드리는
건 더 큰 결정이 필요해 이번엔 보류), 대신 "링크모음" 판별 예시로 프롬프트에 박혀 있던 도메인
목록이 실제 `linkbio_parser`가 지원하는 목록과 따로 놀던 부분(lit.link·taplink는 실제로는
미지원, hity.io/instabio.cc/bio.site/linkon.id/linkseller.net은 지원하는데 누락)을 고쳤다.
`linkbio_parser/hosts.py`에 `SUPPORTED_HOSTS` 공개 상수를 추가하고, `prompts.py`가 여기서
동적으로 예시 문자열을 만들어 쓰게 해서 앞으로 지원 목록이 바뀌면 프롬프트도 자동으로
맞춰진다. 재발 방지 테스트 `tests/test_prompts.py` 신규 추가. **url_type이 코드의 실제 도메인
판단과 별개로 존재한다는 근본 구조 자체는 아직 남아있음** — 스키마/다운스트림 계약을 건드리는
결정이라 별도로 다시 논의할 것.)

(옛 문제 12 — LLM#2 프롬프트가 "재검증 없다"고 실제와 다르게 못박던 문제는 **해결 완료**.
`LINK_SELECTION_SYSTEM`을 "high는 재검증 없이 바로 확정, medium은 실제로 열어봐서 재검증 후
확정, low는 검증 기회 없이 바로 반려"라고 각 등급의 실제 결과를 명시하도록 고쳤다 — "애매하지만
그럴듯하면 low가 아니라 medium을 주라"는 지침도 추가. 프롬프트 텍스트만 바뀌었고 코드/스키마는
그대로라 전체 테스트(390개, 이 시점 기준) 통과 확인. ⚠ 이건 LLM 행동 자체에 영향을 주는 변경이라
실제 운영에서 confidence 분포/해석률이 어떻게 달라지는지는 다음 며칠 daily 실행에서 지켜볼 것.)

(옛 문제 13 — LLM#3 호출 지점(core.py 최초 판별 vs picker.py 재검증)이 같은 응답을 다르게
처리하던 문제는 **해결 완료**. `picker.py`의 medium/force_verify 재검증 분기가 이제
core.py와 동일하게 page_type을 처리한다 — 재검증 대상이 또 다른 링크모음/스토어메인이면
`extract_collection_links`+`finalize_pick`으로 한 홉 더 재귀하고(DOM 추출 전 브라우저 필요
여부도 core.py와 동일하게 처리), "무관"이면 `unresolved` 대신 `hold`로 완화한다. 새 회귀
테스트 4개(`tests/test_picker.py`) 추가 — 재귀로 done까지 도달, 무관→hold, DOM 추출에
브라우저 필요 시 needs_browser로 위임, 하위 링크 추출 실패 시 unresolved. 전체 테스트(394개)
통과 확인. ⚠ 이것도 실제 해석 결과(구제되는 케이스가 늘어남)에 영향을 주는 변경이다.)

(옛 문제 14 — "댓글참여_DM"/"고정댓글_더보기"의 실제 댓글 내용이 전혀 수집되지 않는 문제.
**리서치 완료, 지금 당장은 실익 없음으로 결론.** hifen을 직접 조회해 확인함(2026-08-18):

- 인스타그램은 댓글 테이블 자체가 없다(`SHOW TABLES LIKE '%instagram%'`에 댓글 관련 테이블
  전무) — "댓글참여_DM"은 애초에 실제 구매 링크가 DM으로만 비공개 전달되는 방식이라, 설령
  댓글 테이블이 있었어도 이 카테고리는 원천적으로 해결 불가능했을 것.
- 유튜브는 댓글 테이블이 3개 있다(`YT_video_comments` 61,760건/최신 2025-11-03,
  `video_comments` 19,821건/최신 2023-07-25, `youtube_video_comment_info` 2,088만 건) — 스키마상
  video_id로 실제 댓글 본문을 가져올 수 있는 구조다. 그런데 **실제 데이터로 확인한 결과, 최근
  파이프라인이 다루는 영상(2026-08-14 발행 10건 샘플)에 대해 세 테이블 전부 댓글이 0건**이었다
  — `YT_video_comments`/`video_comments`는 각각 2025-11/2023-07 이후로 갱신이 끊긴 것으로 보이고,
  가장 큰 `youtube_video_comment_info`도 지금 우리가 다루는 최신 영상은 커버하지 않는다.

즉 스키마는 있지만 **우리가 필요로 하는 최신 영상 구간의 실제 댓글 데이터가 없다** — 지금
새 fetch+LLM 단계를 만들어도 입력 자체가 없어 효과가 거의 없을 것으로 판단, 구현하지 않음.
나중에 hifen 쪽에서 최근 영상 댓글 수집이 다시 활성화되면 재검토할 만하다.)

---

*(다음: 지금까지 나온 문제 1~14 중 무엇부터 손볼지 결정 필요. 해결되는 대로 이 문서에서 항목을
지워나갈 것.)*
