"""후반부: 확정 링크(link_status='done')의 상품 상세 수집 — detail/image 테이블 채우기.

전반부(1~9단계)가 "이 포스트가 공구인가 + 구매 링크는 무엇인가"를 확정했다면, 이 단계는
그 확정된 상품페이지를 실제로 크롤링해서 원본 캡션과 합쳐 LLM으로 상세정보를 뽑아
gonggu_{post,video}_product_detail(1:1) / gonggu_{post,video}_product_image(1:N)를 채운다.
DDL은 queries/create_detail_tables.sql 참고.

파이프라인 흐름(상품 1건 기준):
    targets.py   대상 선정 — link_status='done'인데 detail이 없거나 pending/error인 상품
                 (DB 상태가 곧 체크포인트 — 첫 실행은 백로그 백필, 이후는 자동 증분)
               + 원본 캡션을 hifen(SRC) DB에서 배치로 가져옴
    fetchpage.py 확정 candidate_url 1개를 연다 — requests 패스트패스(전체 HTML) →
                 부족하면 브라우저(LazyPage, domain_gate/MAX_BROWSERS 재사용).
                 404/삭제된 상품이면 여기서 gone으로 확정(크롤링 결과 없이 상태만 기록)
    extract.py   코드 추출(결정론적) — 네이버 __PRELOADED_STATE__(naver.py) → JSON-LD/OG →
                 Cafe24 기본정보 테이블 → 배송비 파서(shipping.py) → 이미지(images.py)
    llm.py       LLM#5(상세 요약·판단 필드) 1회 + LLM#4(카테고리, classify_category 재사용) 1회
    validate.py  코드 검증 게이트 — 숫자 존재성/범위/역산/키워드 5개/길이 컷.
                 통과 못 한 필드는 NULL(보수적 원칙: 추측/환각 금지)
    writeback.py detail UPSERT + image 전체 교체(상품 단위 트랜잭션).
                 error/gone은 상태 컬럼만 갱신 — 기존 done 데이터를 NULL로 덮지 않음

실행(저장소 루트에서):
    python3 -m gonggu.enrich_detail                # 대상 전부(첫 실행 = 백로그 전수)
    LIMIT=10 python3 -m gonggu.enrich_detail       # 소량 테스트
    PLATFORM=ig DETAIL_CONCURRENCY=8 python3 -m gonggu.enrich_detail

이미지 URL은 LLM 입출력에서 완전히 배제한다(코드만 다룸) — LLM이 URL을 한 글자만 틀려도
검증 불가능한 깨진 링크가 DB에 들어가기 때문. 상세 원리/결정 기록은 README와
queries/create_detail_tables.sql 상단 주석 참고.
"""
