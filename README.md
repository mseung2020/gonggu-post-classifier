# 공구왕 포스트 분류 파이프라인

인스타그램/유튜브 원본 데이터(hifen DB)에서 "확실한 공구"만 최대한 보수적으로 걸러내
플랫폼별 테이블(dev_gongguking DB의 `gonggu_post`/`gonggu_post_product` — 인스타그램,
`gonggu_video`/`gonggu_video_product` — 유튜브)에 저장하는 파이프라인입니다.

**범위: 여기까지만.** 실제 구매 링크를 프로필/인포크까지 따라가서 최종 상품·가격·이미지를
확정하는 크롤링 작업은 이 테이블을 읽어가는 별도 개발자의 담당이며, 이 저장소에는
포함되지 않습니다. 전체 그림은 `docs/pipeline_diagram.html`을 브라우저로 열어서 보세요.

## 아키텍처

```
hifen DB (읽기 전용, 최근 N일 "공구"/"공동구매" 키워드 매칭 포스트)
   ↓ fetch_source.py
LLM #1 — 공구 여부 판별 + 상품 배열(상품마다 link_location/url_type/urls) + 시작·종료일
   ↓ classify.py
게이트(코드, 보수적) — is_gonggu=false / 상품 특정 실패 / 제휴 광고성 다중 링크 → 제외
   ↓ transform.py
dev_gongguking DB
  - 유튜브: gonggu_video(영상 1건) + gonggu_video_product(상품, 1:N)
  - 인스타그램: gonggu_post(포스트 1건) + gonggu_post_product(상품, 1:N)
   ↑ load.py (이미 있는 video_id/post_id는 재삽입 안 함)
```

컬럼명/타입은 hifen DB의 대응 컬럼(`YT_video_lists.video_id`, `instagram_post.user_id` 등)과
최대한 동일하게 맞춰져 있습니다 — 실제로 조인하진 않지만 봤을 때 바로 알아볼 수 있도록. 자세한
근거는 `queries/create_gonggu_tables.sql` 상단 주석 참고. link_location/url_type/candidate_url은
포스트가 아니라 **상품(product) 테이블**에 있습니다 — 한 포스트에 상품이 여러 개면 상품마다
구매 링크 위치·종류가 다를 수 있기 때문입니다.

## 설치

```bash
pip install -r requirements.txt
cp .env.example .env   # 값 채우기 (DB 자격증명, Dify API 키)
```

## Dify 워크플로우 준비

`dify_workflows/01_gonggu_classify.yml`을 Dify에서 "DSL로 가져오기"로 새 앱으로 import하고,
발급된 API 키를 `.env`의 `DIFY_KEY`에 채웁니다. 모델은 gpt-5-mini 기준으로 프롬프트를
짰습니다(저비용 모델 의도). **참고**: `7월_co_buying_data` 저장소의 "링크방식분류" 앱과
스키마가 다릅니다(`product_hint` 단일 문자열 → `products` 배열) — 반드시 이 yml로 별도
앱을 새로 만들 것, 기존 앱을 덮어쓰지 말 것.

## DB 스키마

`queries/create_gonggu_tables.sql` — dev_gongguking에 적용할 DDL(4개 테이블: gonggu_video,
gonggu_video_product, gonggu_post, gonggu_post_product). 기존 2-테이블 버전을 대체하므로
DROP부터 포함되어 있음 — 이미 넣은 데이터가 있으면 실행 전에 백업할 것.

## 사용법

전체 파이프라인을 한 번에:

```bash
python3 scripts/run_pipeline.py                # fetch → classify → transform → load
DAYS_BACK=14 python3 scripts/run_pipeline.py    # 최근 14일치로
python3 scripts/run_pipeline.py --skip-load     # DB에 안 넣고 load_ready.json까지만 확인
```

단계별로 따로 실행(중간 결과 확인하며 진행하고 싶을 때):

```bash
python3 scripts/fetch_source.py    # data/raw/posts_raw.json
python3 scripts/classify.py        # data/output/classified.json (체크포인트 저장, 이어서 실행 가능)
python3 scripts/transform.py       # data/output/load_ready.json + 제외 사유별 건수 출력
python3 scripts/load.py            # dev_gongguking에 실제 INSERT
```

`scripts/check_db.py` — 소스/타겟 DB 연결과 타겟 테이블 스키마를 확인하는 점검 스크립트.

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
