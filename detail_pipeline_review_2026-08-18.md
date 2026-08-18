# 공구 디테일 일일 퀘스트 재점검 — 2026-08-18

목적: `command.txt`의 "2. 공구 디테일 일일 퀘스트"(`gonggu/enrich_detail/` 패키지, 확정된
링크를 열어 가격/이미지/설명을 보강하는 후반부 단계)를 훑어서 문제점과 해결 방향을 기록한다.
daily_pipeline_review_2026-08-18.md와 같은 방식 — 기존 코드 주석은 참고만 하고 지금 상태
기준으로 새로 판단한다. 이 단계는 아직 `gonggu.daily`에 편입되지 않은 소급(백로그) 전용
단계다(`enrich_detail/__init__.py` 명시).

패키지 구성(19개 파일, 2030줄): `targets.py`(대상선정) → `fetchpage.py`(크롤링+gone/blocked
판정) → `extract.py`+`naver.py`+`shipping.py`+`images.py`(코드 추출) → `llm.py`(LLM#5+#4) →
`validate.py`(코드 우선 검증 게이트) → `writeback.py`(UPSERT). 실행 경로가 두 갈래다:
① `runner.py`(단일실행, 상품 1건마다 크롤링→LLM→DB를 순서대로) ② `crawl_stage.py` →
`llm_stage.py` → `load_stage.py`(2026-08-13 3단계 분리, 백로그가 커서 단일실행이 느릴 때).

---

(옛 문제 1 — load_stage.py에 체크포인트가 없어 매번 detail_llm.jsonl 전체를 DB에 다시
UPSERT하던 문제는 **해결 완료**. `data/output/detail_loaded_keys.jsonl`(다른 done-키
인덱스들과 동일 패턴)을 추가해서, 이미 성공적으로 DB에 반영된 key는 다음 실행에서 건너뛴다
— `write_done`이 실제로 성공했을 때만 그 key를 인덱스에 기록하므로, DB 반영에 실패한 건은
여전히 다음 실행에서 재시도된다. `_todo_records()`로 필터링 로직을 순수 함수로 뽑아 단위
테스트 3개 추가(`tests/test_enrich_detail.py`), 전체 테스트(399개) 통과 확인.)

---

(옛 문제 2 — 배송정보 텍스트 폴백이 이미 LLM용으로 잘린 본문에서 검색되던 문제는 **해결
완료**. `extract.py`의 `_body_text`를 `_clean_body_text`(잘리지 않은 전체 텍스트)와
`_window_body_text`(그걸 LLM#5용으로 자르는 부분)로 분리해서, `_apply_shipping_from_text`는
이제 잘리지 않은 전체 텍스트로 배송 라벨을 찾고 `facts['body_text']`(LLM 컨텍스트)는 예전처럼
그 다음에 윈도잉한다. 회귀 테스트 추가 — 가격-중심 윈도우가 배송 안내를 확실히 배제하도록
구성한 뒤(`'배송정보' not in facts['body_text']`로 테스트 설계 자체를 먼저 검증) 그래도
`shipping_fee`가 정확히 채워지는지 확인. 전체 테스트(400개) 통과 확인.)

---

(옛 문제 3 — crawl_stage.py가 "자기 샤드 출력 파일"만 보고 중복 크롤링 여부를 판단하던
문제는 **해결 완료**. 확정된 실사용 패턴(fast는 항상 단일실행 `enrich_detail`, uc는 항상
`crawl_stage`→`llm_stage`→`load_stage`, uc `crawl_stage`는 5-way 샤딩을 쓸 때도/안 쓸
때도 있음)을 반영해 재확인한 결과, 진짜 문제는 "단일실행 vs 3단계 분리"가 아니라
**`crawl_stage.py` 자체가 `SHARD_COUNT`에 따라 결과를 쓰는 파일이 달라지는데(`detail_crawled.jsonl`
↔ `detail_crawled_shard0~4.jsonl`) "이미 크롤링했는지" 체크는 자기가 쓸 그 파일 하나만
봤다는 것**이었다. 샤딩 유무/개수를 실행마다 바꾸면(정확히 지금 실사용 패턴), 다른 샤드
구성으로 이미 성공적으로 크롤링해둔 상품(느리고 사람이 지켜봐야 하는 uc로 어렵게 뚫어낸
것 포함)을 못 보고 또 크롤링하게 된다.

`crawl_stage.py`에 `_all_crawled_keys()`를 추가해 `detail_crawled*.jsonl` 전부를 합쳐서
(`llm_stage.py._load_crawled()`와 동일한 방식) "이미 크롤링됐는지"를 판단하도록 고쳤다 —
이번 실행 결과를 쓰는 파일은 그대로 자기 샤드 파일이지만, 중복 체크는 전체를 본다. 회귀
테스트 2개(`tests/test_enrich_detail.py`) 추가, 전체 테스트(396개) 통과 확인.

한편 "단일실행(fast)과 3단계 분리(uc)를 섞어 쓰면 중복 작업이 생기지 않냐"는 원래 걱정은,
실사용 패턴을 보니 fast(단일실행)와 uc(3단계 분리)가 DB에서 보는 대상 자체가 겹치지 않아
(`targets.py`: fast는 `detail_status`가 없거나 pending/error인 것만, uc는 `blocked`인 것만 —
상호 배타적) 실질적인 위험이 아니었다.)

---

## 문제 4(확신도 낮음) — Cafe24 할인율 셀렉터가 너무 넓어서 무관한 숫자를 주울 수 있음

**현상**: `extract.py:_apply_cafe24`의 할인율 폴백:
```python
for node in soup.select(".discount_rate, .rate, [class*='discount']"):
    ...
    m = re.search(r'(\d{1,3})\s*%', txt)
    if m:
        facts['discount_rate'] = int(m.group(1))
```
`[class*='discount']`는 클래스명에 "discount"가 들어간 **모든** 요소를 매칭한다 — 쿠폰
배너("추가 10% 할인 쿠폰"), 이벤트 문구 등 이 상품의 실제 할인율과 무관한 퍼센트를 주울
위험이 있다. `validate.py`의 검증 게이트는 LLM이 낸 숫자만 "입력 텍스트에 실제로 있는지"
교차검증하고, **코드가 뽑은 값(`facts['discount_rate']`)은 0~100 범위 체크만 하고 무조건
신뢰**하므로(`merge_and_validate`), 이 셀렉터가 잘못 주운 값을 걸러낼 안전망이 없다.

**조사 결과(2026-08-18) — 이번엔 손대지 않음**: 실제 오탐 사례를 확인할 방법이 이 환경에는
없었다(실제 Cafe24 페이지를 열어봐야 하는데 그럴 대상 URL/과거 오탐 로그가 없음). 기존
테스트(`test_cafe24_spec_table_and_discount`)가 검증하는 정상 케이스는 셀렉터를 좁혀도
그대로 통과하므로, 지금 셀렉터를 좁히는 건 "확인된 문제를 고치는 것"이 아니라 "이론적
위험에 대비해 커버리지를 줄이는 것"이 되어 확신 없이 코드를 바꾸는 셈이다 — 이 저장소가
일관되게 지켜온 "실측 근거 없이 미리 고치지 않는다"는 원칙과 맞지 않아 보류한다.

**실제로 의심되는 상황이 생기면**: `python3 -m gonggu.enrich_detail._diag_url "<의심되는 URL>"`
로 그 페이지를 직접 열어 `extract_facts`가 어느 값을 뽑는지 확인할 것 — 이 스크립트가 이미
그 목적으로 만들어져 있다. 실제 오탐이 확인되면 셀렉터를 좁히거나(예: 상품 가격 영역 안에
있는 요소만) 코드값에도 가벼운 교차검증을 추가하는 걸 그때 다시 고려한다.

---

## 문제없음으로 확인

- **gone/blocked/error 3분류와 그 처리 차이**: `fetchpage.py`가 "영구 소멸(재시도 무의미)" /
  "안티봇 차단(fast는 포기, uc 패스로)" / "일시 실패(fast가 재시도)"를 명확히 구분하고,
  `writeback.py`의 `write_status`(상태만 갱신) vs `write_done`(데이터+상태)를 분리해서 **이미
  done인 상품이 재크롤링 실패로 데이터까지 NULL로 덮이는 사고를 구조적으로 막는다** — 실측
  사고 방지 원칙이 코드에 잘 반영됨.
- **이미지 URL의 LLM 완전 배제**: `llm.py`/`validate.py` 어디에도 이미지 URL이 LLM 입출력에
  안 섞이게 설계되어 있어(`_facts_line`이 의도적으로 제외), "URL 한 글자 틀려서 깨진 링크가
  DB에 들어가는" 위험 자체가 구조적으로 없음.
- **가격 검증(존재성 + 배율 상한)**: `validate.py`의 `number_in_text`(LLM 숫자가 입력에 실제로
  있는지) + `MAX_PRICE_MULTIPLE=20`(정가가 판매가의 20배 넘으면 오추출로 버림) 조합이 실제
  발견된 사고(그로우러닝 유아교구 정가 오추출 등)에 정확히 대응해서 설계됨.
- **naver.py의 __PRELOADED_STATE__ 파싱**: 중괄호 균형+문자열 이스케이프 인식으로 객체 경계를
  찾는 방식이 견고하고, undefined/NaN 같은 JS 리터럴 치환도 빠짐없이 처리.
- **llm_stage.py의 체크포인트**: 샤딩된 여러 `detail_crawled_shard*.jsonl`을 처음부터 전부
  합쳐서(`_load_crawled`) 처리하는 방식이 처음부터 올바르게 설계됨(옛 문제 3은 `crawl_stage.py`
  쪽에서만 이 방식을 안 따르고 있었던 것).

---

*(다음: 문제 1~4 중 무엇부터 손볼지, 아니면 daily_pipeline_review와 마찬가지로 순서를 먼저
정할지 결정 필요.)*
