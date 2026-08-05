-- 공구왕 "상품 상세" 테이블 — link_status='done'인 상품(구매 링크가 확정된 것)만 대상으로,
-- 그 확정 링크를 실제로 열어서 크롤링한 상세페이지 정보 + 원본 캡션/디스크립션을 종합한 LLM 요약을
-- 저장한다. create_gonggu_tables.sql의 4개 테이블(gonggu_video/gonggu_video_product/
-- gonggu_post/gonggu_post_product)이 이미 있어야 이 파일을 실행할 수 있다(FK 대상).
--
-- 설계 원칙:
--  1) create_gonggu_tables.sql과 마찬가지로 플랫폼별로 테이블을 분리한다 — 인스타그램
--     (gonggu_post_product_*)과 유튜브(gonggu_video_product_*) 스키마는 완전히 동일하되
--     FK만 각 플랫폼의 product 테이블을 향한다.
--  2) 상세 정보(가격/배송/브랜드/카테고리/AI요약)는 상품 1건당 1행(_detail, 재크롤링 시 UPDATE),
--     이미지는 1건당 N행(_image)으로 분리한다 — 이미지 개수가 가변이라 1:1로 억지로 합치지 않음.
--  3) category/subcategory는 이미 운영 중인 LLM#4(scripts/classify_category.py,
--     common.CATEGORY_TAXONOMY)의 출력 필드명과 동일하게 맞춘다 — 그 결과를 옮겨 담을 때
--     이름 매핑이 필요 없도록.
--  4) 검색 키워드 5개는 candidate_url의 세미콜론 컨벤션과 통일해 한 컬럼에 쉼표(,)로
--     이어붙인다(정확히 5개라는 제약은 스키마가 아니라 적재 코드 책임).
--  5) 이미지 용도(썸네일 vs 상세설명 이미지) 구분은 sort_order가 아니라 별도 컬럼
--     image_type으로 명시한다 — sort_order는 같은 image_type 안에서의 순서만 담당.
--  6) 이 파일은 신규 설치용이며 DROP을 포함하지 않는다 — 이미 있는 DB에 이 파일만 추가로
--     실행해도(ALTER 없이) 안전하게 새 테이블만 생성된다.

-- ============================================================
-- 1) gonggu_post_product_detail — gonggu_post_product 1건당 1행.
-- ============================================================
CREATE TABLE gonggu_post_product_detail (
    id                      BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    post_product_id         BIGINT UNSIGNED NOT NULL
                            COMMENT 'gonggu_post_product.id FK. 1상품당 1행 — 재크롤링해도 일단은 새 행을 만들지 않고 이 행을 UPDATE',
    thumbnail_url           VARCHAR(500) NULL
                            COMMENT '대표 이미지(=gonggu_post_product_image에서 image_type=''thumbnail''인 행과 동일 값, 조인 없이 바로 쓰라고 의도적으로 이 테이블에도 중복 저장)',
    brand_name_ko           VARCHAR(100) NULL COMMENT '브랜드 한글명칭. 조회 불가하면 NULL',
    brand_name_en           VARCHAR(150) NULL COMMENT '브랜드 영어명칭. 조회 불가하면 NULL',
    category                VARCHAR(30) NULL
                            COMMENT '대카테고리. LLM#4(scripts/classify_category.py, common.CATEGORY_TAXONOMY)가 만든 임시 사이드 분류 결과를 그대로 옮김 — 필드명을 그 결과와 동일하게 맞춤',
    subcategory             VARCHAR(50) NULL COMMENT '하위카테고리. category와 마찬가지로 LLM#4 결과, CATEGORY_TAXONOMY[category] 목록 중 하나',
    search_keywords         VARCHAR(300) NULL COMMENT '검색용 키워드 5개, 쉼표(,)로 구분(예: "냉감이불,쿨링패드,여름침구,접촉냉감,침구공구")',
    original_price          INT UNSIGNED NULL COMMENT '정가(할인 전)',
    sale_price              INT UNSIGNED NULL COMMENT '판매가(공구가, 할인 후)',
    discount_rate           TINYINT UNSIGNED NULL COMMENT '할인율(%), 0~100. 페이지에 직접 표기된 값 우선, 없으면 가격으로 역산',
    discount_amount         INT UNSIGNED NULL COMMENT '절약 금액(원). 페이지에 직접 표기된 값 우선, 없으면 가격으로 역산',
    free_shipping           INT NULL COMMENT '무료배송 여부. 0아니면 1',
    shipping_fee            INT UNSIGNED NULL COMMENT '유료배송이면 배송비(원)',
    shipping_note           VARCHAR(200) NULL COMMENT '배송 관련 자유서술 1건(예: "제주/도서산간 추가", "새벽배송 가능")',
    composition_info        VARCHAR(300) NULL COMMENT '구성 요약 1건(예: "2개 세트", "본품+리필 1개") — 옵션별 개별 가격까지는 관리 안 함(포스트=상품 1개 원칙과 동일하게 대표 구성만)',
    gift_info                VARCHAR(300) NULL COMMENT '사은품 요약 1건(예: "미니어처 4종")',
    coupon_info              VARCHAR(300) NULL COMMENT '쿠폰/중복할인 요약 1건(예: "5% 중복 쿠폰")',
    ai_summary               VARCHAR(1000) NULL COMMENT '크롤링 결과 + 원본 캡션을 종합해 LLM이 생성한 공구 요약',
    ai_summary_confidence    TINYINT UNSIGNED NULL COMMENT 'AI 요약 신뢰도(%), 0~100',
    detail_status            ENUM('pending', 'done', 'error') NOT NULL DEFAULT 'pending'
                            COMMENT '이 행의 상세 크롤링/요약 처리 상태 — link_status(pending/done/unresolved) 컨벤션과 동일한 취지',
    detail_error             VARCHAR(500) NULL COMMENT '실패 사유(LLM 단계 에러 메시지 로그)',
    created_at              DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at              DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
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
    id                  BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    post_product_id     BIGINT UNSIGNED NOT NULL COMMENT 'gonggu_post_product.id FK',
    image_url           VARCHAR(500) NOT NULL,
    image_type          ENUM('thumbnail', 'detail') NOT NULL DEFAULT 'detail'
                        COMMENT '이미지 용도 — thumbnail: 주로 정사각형 대표 이미지(절대는 아님), detail: 주로 세로로 긴 상세설명 이미지(절대는 아님)',
    sort_order          TINYINT UNSIGNED NOT NULL DEFAULT 0
                        COMMENT '같은 image_type 안에서의 화면 내 순서(0부터)',
    created_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
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
    id                      BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    video_product_id        BIGINT UNSIGNED NOT NULL
                            COMMENT 'gonggu_video_product.id FK. 1상품당 1행 — 재크롤링해도 일단은 새 행을 만들지 않고 이 행을 UPDATE',
    thumbnail_url           VARCHAR(500) NULL
                            COMMENT '대표 이미지(=gonggu_video_product_image에서 image_type=''thumbnail''인 행과 동일 값, 조인 없이 바로 쓰라고 의도적으로 이 테이블에도 중복 저장)',
    brand_name_ko           VARCHAR(100) NULL COMMENT '브랜드 한글명칭. 조회 불가하면 NULL',
    brand_name_en           VARCHAR(150) NULL COMMENT '브랜드 영어명칭. 조회 불가하면 NULL',
    category                VARCHAR(30) NULL
                            COMMENT '대카테고리. LLM#4(scripts/classify_category.py, common.CATEGORY_TAXONOMY)가 만든 임시 사이드 분류 결과를 그대로 옮김 — 필드명을 그 결과와 동일하게 맞춤',
    subcategory             VARCHAR(50) NULL COMMENT '하위카테고리. category와 마찬가지로 LLM#4 결과, CATEGORY_TAXONOMY[category] 목록 중 하나',
    search_keywords         VARCHAR(300) NULL COMMENT '검색용 키워드 5개, 쉼표(,)로 구분(예: "냉감이불,쿨링패드,여름침구,접촉냉감,침구공구")',
    original_price          INT UNSIGNED NULL COMMENT '정가(할인 전)',
    sale_price              INT UNSIGNED NULL COMMENT '판매가(공구가, 할인 후)',
    discount_rate           TINYINT UNSIGNED NULL COMMENT '할인율(%), 0~100. 페이지에 직접 표기된 값 우선, 없으면 가격으로 역산',
    discount_amount         INT UNSIGNED NULL COMMENT '절약 금액(원). 페이지에 직접 표기된 값 우선, 없으면 가격으로 역산',
    free_shipping           INT NULL COMMENT '무료배송 여부. 0아니면 1',
    shipping_fee            INT UNSIGNED NULL COMMENT '유료배송이면 배송비(원)',
    shipping_note           VARCHAR(200) NULL COMMENT '배송 관련 자유서술 1건(예: "제주/도서산간 추가", "새벽배송 가능")',
    composition_info        VARCHAR(300) NULL COMMENT '구성 요약 1건(예: "2개 세트", "본품+리필 1개") — 옵션별 개별 가격까지는 관리 안 함(포스트=상품 1개 원칙과 동일하게 대표 구성만)',
    gift_info                VARCHAR(300) NULL COMMENT '사은품 요약 1건(예: "미니어처 4종")',
    coupon_info              VARCHAR(300) NULL COMMENT '쿠폰/중복할인 요약 1건(예: "5% 중복 쿠폰")',
    ai_summary               VARCHAR(1000) NULL COMMENT '크롤링 결과 + 원본 캡션을 종합해 LLM이 생성한 공구 요약',
    ai_summary_confidence    TINYINT UNSIGNED NULL COMMENT 'AI 요약 신뢰도(%), 0~100',
    detail_status            ENUM('pending', 'done', 'error') NOT NULL DEFAULT 'pending'
                            COMMENT '이 행의 상세 크롤링/요약 처리 상태 — link_status(pending/done/unresolved) 컨벤션과 동일한 취지',
    detail_error             VARCHAR(500) NULL COMMENT '실패 사유(LLM 단계 에러 메시지 로그)',
    created_at              DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at              DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
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
    id                  BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    video_product_id    BIGINT UNSIGNED NOT NULL COMMENT 'gonggu_video_product.id FK',
    image_url           VARCHAR(500) NOT NULL,
    image_type          ENUM('thumbnail', 'detail') NOT NULL DEFAULT 'detail'
                        COMMENT '이미지 용도 — thumbnail: 주로 정사각형 대표 이미지(절대는 아님), detail: 주로 세로로 긴 상세설명 이미지(절대는 아님)',
    sort_order          TINYINT UNSIGNED NOT NULL DEFAULT 0
                        COMMENT '같은 image_type 안에서의 화면 내 순서(0부터)',
    created_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_gonggu_video_product_image_product (video_product_id, image_type, sort_order),
    CONSTRAINT fk_gonggu_video_product_image_product
        FOREIGN KEY (video_product_id) REFERENCES gonggu_video_product (id)
        ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='gonggu_video_product 상세페이지 갤러리 이미지(1:N), image_type으로 썸네일/상세이미지 구분';
