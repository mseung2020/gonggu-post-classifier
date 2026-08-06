-- ============================================================
-- 후반부(상세 수집) 테이블 4개 — dev_gongguking에 적용하는 DDL.
--
-- 대상: link_status='done'으로 구매 링크가 확정된 상품.
-- gonggu/enrich_detail 단계가 그 확정 링크를 크롤링해 이 테이블들을 채운다.
--   - *_detail: 상품당 1행(1:1, UNIQUE). 재크롤링 시 새 행을 만들지 않고 UPDATE.
--   - *_image : 상세페이지 갤러리 이미지(1:N). image_type으로 썸네일/상세 구분.
--
-- detail_status 규약(link_status의 pending/done/unresolved 컨벤션과 같은 취지):
--   pending = 아직 처리 안 됨(기본값)
--   done    = 크롤링+LLM 요약까지 완료
--   error   = 일시 실패(타임아웃/LLM 에러 등) → 다음 실행에서 자동 재시도
--   gone    = 페이지 영구 소멸(404/판매종료/상품삭제) → 재시도하지 않음.
--             "죽은 링크도 기록은 남긴다" 방침(2026-08-06)에 따라 행 자체는 생성.
--
-- 신규 설치용이며 DROP을 포함하지 않는다 — 테이블이 이미 있으면 에러로 멈출 뿐
-- 기존 데이터는 건드리지 않는다(안전한 실패). create_gonggu_tables.sql과 같은 규약.
-- 이미 'gone' 없는 구버전 ENUM으로 생성된 경우에만 아래 ALTER를 대신 실행:
--   ALTER TABLE gonggu_post_product_detail  MODIFY COLUMN detail_status
--     ENUM('pending','done','error','gone') NOT NULL DEFAULT 'pending';
--   ALTER TABLE gonggu_video_product_detail MODIFY COLUMN detail_status
--     ENUM('pending','done','error','gone') NOT NULL DEFAULT 'pending';
-- ============================================================

-- ============================================================
-- 1) gonggu_post_product_detail — gonggu_post_product 1건당 1행.
-- ============================================================
CREATE TABLE gonggu_post_product_detail (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    post_product_id BIGINT UNSIGNED NOT NULL COMMENT 'gonggu_post_product.id FK. 1상품당 1행 — 재크롤링해도 일단은 새 행을 만들지 않고 이 행을 UPDATE',
    thumbnail_url VARCHAR(500) NULL COMMENT '대표 이미지(=gonggu_post_product_image에서 image_type=''thumbnail''인 행과 동일 값, 조인 없이 바로 쓰라고 의도적으로 이 테이블에도 중복 저장)',
    brand_name_kr VARCHAR(100) NULL COMMENT '브랜드 한글명칭. 조회 불가하면 NULL',
    brand_name_en VARCHAR(150) NULL COMMENT '브랜드 영어명칭. 조회 불가하면 NULL',
    category VARCHAR(30) NULL COMMENT '대카테고리. LLM#4(scripts/classify_category.py, common.CATEGORY_TAXONOMY)가 만든 임시 사이드 분류 결과를 그대로 옮김 — 필드명을 그 결과와 동일하게 맞춤',
    subcategory VARCHAR(50) NULL COMMENT '하위카테고리. category와 마찬가지로 LLM#4 결과, CATEGORY_TAXONOMY[category] 목록 중 하나',
    search_keywords VARCHAR(300) NULL COMMENT '검색용 키워드 5개, 쉼표(,)로 구분(예: "냉감이불,쿨링패드,여름침구,접촉냉감,침구공구")',
    original_price INT UNSIGNED NULL COMMENT '정가(할인 전)',
    sale_price INT UNSIGNED NULL COMMENT '판매가(공구가, 할인 후)',
    discount_rate TINYINT UNSIGNED NULL COMMENT '할인율(%), 0~100. 페이지에 직접 표기된 값 우선, 없으면 가격으로 역산',
    discount_amount INT UNSIGNED NULL COMMENT '절약 금액(원). 페이지에 직접 표기된 값 우선, 없으면 가격으로 역산',
    free_shipping INT NULL COMMENT '무료배송 여부. 0아니면 1',
    shipping_fee INT UNSIGNED NULL COMMENT '유료배송이면 배송비(원)',
    shipping_note VARCHAR(200) NULL COMMENT '배송 관련 자유서술 1건(예: "제주/도서산간 추가", "새벽배송 가능")',
    composition_info VARCHAR(300) NULL COMMENT '구성 요약 1건(예: "2개 세트", "본품+리필 1개") — 옵션별 개별 가격까지는 관리 안 함(포스트=상품 1개 원칙과 동일하게 대표 구성만)',
    gift_info VARCHAR(300) NULL COMMENT '사은품 요약 1건(예: "미니어처 4종")',
    coupon_info VARCHAR(300) NULL COMMENT '쿠폰/중복할인 요약 1건(예: "5% 중복 쿠폰")',
    ai_summary VARCHAR(1000) NULL COMMENT '크롤링 결과 + 원본 캡션을 종합해 LLM이 생성한 공구 요약',
    ai_summary_confidence TINYINT UNSIGNED NULL COMMENT 'AI 요약 신뢰도(%), 0~100',
    detail_status ENUM('pending', 'done', 'error', 'gone') NOT NULL DEFAULT 'pending' COMMENT '처리 상태. error=일시 실패(다음 실행 때 자동 재시도), gone=페이지 영구 소멸(404/판매종료/삭제 — 재시도 안 함)',
    detail_error VARCHAR(500) NULL COMMENT '실패 사유(LLM 단계 에러 메시지 로그)',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_gonggu_post_product_detail_product (post_product_id),
    KEY idx_gonggu_post_product_detail_status (detail_status),
    KEY idx_gonggu_post_product_detail_category (category, subcategory),
    CONSTRAINT fk_gonggu_post_product_detail_product
        FOREIGN KEY (post_product_id) REFERENCES gonggu_post_product (id)
        ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='gonggu_post_product 1건의 상세페이지 크롤링 결과(1:1) — link_status=done인 상품만 대상';

-- ============================================================
-- 2) gonggu_post_product_image — 상세페이지 갤러리 이미지(1:N).
-- ============================================================
CREATE TABLE gonggu_post_product_image (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    post_product_id BIGINT UNSIGNED NOT NULL COMMENT 'gonggu_post_product.id FK',
    image_url VARCHAR(500) NOT NULL,
    image_type ENUM('thumbnail', 'detail') NOT NULL DEFAULT 'detail' COMMENT '이미지 용도 — thumbnail: 주로 정사각형 대표 이미지(절대는 아님), detail: 주로 세로로 긴 상세설명 이미지(절대는 아님)',
    sort_order TINYINT UNSIGNED NOT NULL DEFAULT 0 COMMENT '같은 image_type 안에서의 화면 내 순서(0부터)',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_gonggu_post_product_image_product (post_product_id, image_type, sort_order),
    CONSTRAINT fk_gonggu_post_product_image_product
        FOREIGN KEY (post_product_id) REFERENCES gonggu_post_product (id)
        ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='gonggu_post_product 상세페이지 갤러리 이미지(1:N), image_type으로 썸네일/상세이미지 구분';

-- ============================================================
-- 3) gonggu_video_product_detail — gonggu_video_product 1건당 1행.
--    gonggu_post_product_detail과 스키마가 완전히 동일하며 FK만 gonggu_video_product를 향한다.
-- ============================================================
CREATE TABLE gonggu_video_product_detail (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    video_product_id BIGINT UNSIGNED NOT NULL COMMENT 'gonggu_video_product.id FK. 1상품당 1행 — 재크롤링해도 일단은 새 행을 만들지 않고 이 행을 UPDATE',
    thumbnail_url VARCHAR(500) NULL COMMENT '대표 이미지(=gonggu_video_product_image에서 image_type=''thumbnail''인 행과 동일 값, 조인 없이 바로 쓰라고 의도적으로 이 테이블에도 중복 저장)',
    brand_name_kr VARCHAR(100) NULL COMMENT '브랜드 한글명칭. 조회 불가하면 NULL',
    brand_name_en VARCHAR(150) NULL COMMENT '브랜드 영어명칭. 조회 불가하면 NULL',
    category VARCHAR(30) NULL COMMENT '대카테고리. LLM#4(scripts/classify_category.py, common.CATEGORY_TAXONOMY)가 만든 임시 사이드 분류 결과를 그대로 옮김 — 필드명을 그 결과와 동일하게 맞춤',
    subcategory VARCHAR(50) NULL COMMENT '하위카테고리. category와 마찬가지로 LLM#4 결과, CATEGORY_TAXONOMY[category] 목록 중 하나',
    search_keywords VARCHAR(300) NULL COMMENT '검색용 키워드 5개, 쉼표(,)로 구분(예: "냉감이불,쿨링패드,여름침구,접촉냉감,침구공구")',
    original_price INT UNSIGNED NULL COMMENT '정가(할인 전)',
    sale_price INT UNSIGNED NULL COMMENT '판매가(공구가, 할인 후)',
    discount_rate TINYINT UNSIGNED NULL COMMENT '할인율(%), 0~100. 페이지에 직접 표기된 값 우선, 없으면 가격으로 역산',
    discount_amount INT UNSIGNED NULL COMMENT '절약 금액(원). 페이지에 직접 표기된 값 우선, 없으면 가격으로 역산',
    free_shipping INT NULL COMMENT '무료배송 여부. 0아니면 1',
    shipping_fee INT UNSIGNED NULL COMMENT '유료배송이면 배송비(원)',
    shipping_note VARCHAR(200) NULL COMMENT '배송 관련 자유서술 1건(예: "제주/도서산간 추가", "새벽배송 가능")',
    composition_info VARCHAR(300) NULL COMMENT '구성 요약 1건(예: "2개 세트", "본품+리필 1개") — 옵션별 개별 가격까지는 관리 안 함(포스트=상품 1개 원칙과 동일하게 대표 구성만)',
    gift_info VARCHAR(300) NULL COMMENT '사은품 요약 1건(예: "미니어처 4종")',
    coupon_info VARCHAR(300) NULL COMMENT '쿠폰/중복할인 요약 1건(예: "5% 중복 쿠폰")',
    ai_summary VARCHAR(1000) NULL COMMENT '크롤링 결과 + 원본 캡션을 종합해 LLM이 생성한 공구 요약',
    ai_summary_confidence TINYINT UNSIGNED NULL COMMENT 'AI 요약 신뢰도(%), 0~100',
    detail_status ENUM('pending', 'done', 'error', 'gone') NOT NULL DEFAULT 'pending' COMMENT '처리 상태. error=일시 실패(다음 실행 때 자동 재시도), gone=페이지 영구 소멸(404/판매종료/삭제 — 재시도 안 함)',
    detail_error VARCHAR(500) NULL COMMENT '실패 사유(LLM 단계 에러 메시지 로그)',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_gonggu_video_product_detail_product (video_product_id),
    KEY idx_gonggu_video_product_detail_status (detail_status),
    KEY idx_gonggu_video_product_detail_category (category, subcategory),
    CONSTRAINT fk_gonggu_video_product_detail_product
        FOREIGN KEY (video_product_id) REFERENCES gonggu_video_product (id)
        ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='gonggu_video_product 1건의 상세페이지 크롤링 결과(1:1) — link_status=done인 상품만 대상';

-- ============================================================
-- 4) gonggu_video_product_image — 상세페이지 갤러리 이미지(1:N).
-- ============================================================
CREATE TABLE gonggu_video_product_image (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    video_product_id BIGINT UNSIGNED NOT NULL COMMENT 'gonggu_video_product.id FK',
    image_url VARCHAR(500) NOT NULL,
    image_type ENUM('thumbnail', 'detail') NOT NULL DEFAULT 'detail' COMMENT '이미지 용도 — thumbnail: 주로 정사각형 대표 이미지(절대는 아님), detail: 주로 세로로 긴 상세설명 이미지(절대는 아님)',
    sort_order TINYINT UNSIGNED NOT NULL DEFAULT 0 COMMENT '같은 image_type 안에서의 화면 내 순서(0부터)',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_gonggu_video_product_image_product (video_product_id, image_type, sort_order),
    CONSTRAINT fk_gonggu_video_product_image_product
        FOREIGN KEY (video_product_id) REFERENCES gonggu_video_product (id)
        ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='gonggu_video_product 상세페이지 갤러리 이미지(1:N), image_type으로 썸네일/상세이미지 구분';
