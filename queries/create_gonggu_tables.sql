-- 공구왕 "확정 공구" 테이블 — 다운스트림 개발자(프로필/인포크 링크 크롤링 담당)가
-- 이 네 테이블만 보고 이후 파이프라인(링크 해석 → 최종 상품/가격/이미지 확정)을 전부 진행함.
--
-- 설계 원칙(피드백 반영):
--  1) 플랫폼별로 부모 테이블을 분리한다 — gonggu_video(유튜브) / gonggu_post(인스타그램).
--     하나로 통합하지 않는 이유: 두 플랫폼의 원본 테이블(YT_video_lists vs instagram_post)
--     자체가 컬럼명·타입 컨벤션이 서로 다르므로(publishDate DATE vs publish_date DATETIME 등),
--     억지로 통합하면 오히려 각 원본과의 매칭이 흐려짐.
--  2) 각 컬럼명/타입은 "우리 DB에서 실제로 조인할 일은 없지만, 봤을 때 바로 알아볼 수 있게"
--     원본 hifen DB의 대응 컬럼과 최대한 동일하게 맞춘다.
--     - gonggu_video.video_id      ↔ YT_video_lists.video_id       VARCHAR(50)
--     - gonggu_video.channel_id    ↔ YT_video_lists.channel_id     VARCHAR(50)
--     - gonggu_video.publishDate   ↔ YT_video_lists.publishDate    DATE (카멜케이스까지 그대로)
--     - gonggu_video.video_url     ↔ brand.video_url               VARCHAR(100)
--     - gonggu_post.post_id        ↔ instagram_post.post_id        VARCHAR(50)
--     - gonggu_post.user_id        ↔ instagram_post.user_id / instagram_user.user_id  VARCHAR(50)
--     - gonggu_post.url            ↔ instagram_post.url            VARCHAR(300)
--     - gonggu_post.publish_date   ↔ instagram_post.publish_date   DATETIME
--     (공구 시작일/종료일·분류 특이사항처럼 원본에 대응 컬럼이 없는 건 새로 이름 지음)
--     channel_id/user_id는 원본에서는 nullable이지만, 여기서는 "유저 필수" 요건이 있어 NOT NULL로 둠.
--  3) 부모→자식(상품) 연결 컬럼명은 두 쌍(video/video_product, post/post_product) 모두 동일하게
--     gonggu_id로 통일한다.
--  4) 중복되면 안 되는 값(video_id, post_id)에는 UNIQUE. product_name은 걸지 않음
--     (다른 크리에이터가 같은 상품을 공구하는 경우가 있을 수 있고, 같은 포스트 안에서도
--     굳이 강제할 이유가 없음 — id/gonggu_id만으로 관리).
--  5) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci.
--  6) link_location/url_type/candidate_url은 "한 포스트에 상품이 여러 개면 상품마다 구매 경로가
--     다를 수 있다"는 이유로 부모가 아니라 상품(product) 테이블 쪽에 둔다.
--
-- 참고: MySQL은 인접한 문자열 리터럴을 자동으로 이어붙이지 않으므로, 모든 COMMENT는
-- 하나의 문자열 리터럴로 작성한다.

-- 이전 버전(2-테이블 통합형 + caption_preview 있던 4-테이블형)을 전부 대체한다.
-- 이미 넣은 데이터가 없다는 전제 — 데이터가 있다면 DROP 전에 반드시 백업할 것.
-- 자식(FK 있는 쪽) 먼저 DROP.
DROP TABLE IF EXISTS gonggu_product;
DROP TABLE IF EXISTS gonggu_video_product;
DROP TABLE IF EXISTS gonggu_post_product;
DROP TABLE IF EXISTS gonggu_video;
DROP TABLE IF EXISTS gonggu_post;

-- ============================================================
-- 1) gonggu_video — 공구가 "확실한"(최대한 보수적으로 필터링된) 유튜브 영상 1건당 1행
-- ============================================================
CREATE TABLE gonggu_video (
    id                  BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    video_id            VARCHAR(50) NOT NULL
                        COMMENT 'YT_video_lists.video_id와 동일 컬럼명·타입 (ex. 8af8hqQYaE)',
    channel_id          VARCHAR(50) NOT NULL
                        COMMENT 'YT_video_lists.channel_id와 동일 컬럼명·타입. 원본은 nullable이지만 여기서는 필수(ex. UCQZodgu6RuPz2uw-nnrGfcA)',
    title               VARCHAR(300) NULL
                        COMMENT 'YT_video_lists.title과 동일 컬럼명·타입',
    video_url           VARCHAR(100) NULL
                        COMMENT 'brand.video_url과 동일 컬럼명·타입 (ex. https://www.youtube.com/watch?v=8af8hqQYaE)',
    publishDate         DATE NOT NULL
                        COMMENT 'YT_video_lists.publishDate와 동일 컬럼명(카멜케이스 그대로)·타입 — 캡션의 "오늘/내일" 상대 날짜 표현을 해석할 때 기준점으로 씀',
    gonggu_start_date   DATE NULL
                        COMMENT '공구 시작일. 캡션에 명시적 날짜가 있거나 게시일 기준 상대표현이 명확할 때만 채움 — 특정 불가능하면 NULL(추측/환각 금지)',
    gonggu_end_date     DATE NULL
                        COMMENT '공구 종료일(마감일). 계산 기준은 gonggu_start_date와 동일',
    classification_note VARCHAR(500) NULL
                        COMMENT 'LLM 분류 단계에서 남긴 특이사항 자유서술(500자 이내, 예: "본문엔 상담용 채널톡만 있고 실제 구매는 프로필 링크트리 경유")',
    created_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_gonggu_video_video_id (video_id),
    KEY idx_gonggu_video_channel (channel_id),
    KEY idx_gonggu_video_end_date (gonggu_end_date),
    KEY idx_gonggu_video_publish_date (publishDate)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='공구가 확실한 것만 보수적으로 필터링한 유튜브 영상. 링크 해석 파이프라인의 입력 테이블';

-- ============================================================
-- 2) gonggu_video_product — 한 공구 영상이 홍보하는 상품(1:N).
--    같은 영상 안에서도 상품마다 구매 링크 위치/종류가 다를 수 있어 이 테이블에 둔다.
-- ============================================================
CREATE TABLE gonggu_video_product (
    id             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    gonggu_id      VARCHAR(50) NOT NULL
                   COMMENT 'gonggu_video.video_id FK(자연키). gonggu_post_product.gonggu_id(→gonggu_post.post_id)와 동일한 이름·타입으로 통일',
    product_name   VARCHAR(300) NOT NULL
                   COMMENT '상품명(캡션/설명에서 추출한 그대로, 브랜드명 포함 권장)',
    link_location   ENUM('설명_직접링크', '설명_프로필안내', '댓글참여_DM', '고정댓글_더보기', '링크없음_불명') NOT NULL
                   COMMENT '이 상품의 구매 링크가 어디 있는지 — 다운스트림 크롤링 시작점 힌트. "댓글참여_DM"이어도 프로필에 상시 링크(인포크 등)가 따로 있는 경우가 많으니 이 값만으로 크롤링 대상에서 제외하지 말 것',
    url_type        VARCHAR(30) NULL
                   COMMENT '대표 구매 URL 종류 — 네이버_스마트스토어 / 네이버_기타 / 링크모음 / 자사몰_독립몰 / 카카오채널 / 쿠팡_오픈마켓 / 결제플랫폼 / 단축링크 / 기타 / 없음 중 하나(자유 텍스트, 값 늘어날 수 있어 ENUM 대신 VARCHAR)',
    candidate_url   VARCHAR(500) NULL
                   COMMENT '캡션/프로필에서 1차로 발견된 이 상품의 구매 링크 후보(참고용, 미검증) — 실제 최종 링크 확정은 다운스트림 크롤링 단계 책임. 여러 개면 세미콜론(;)으로 구분, 원본부터 "..."로 잘려 있으면 그대로 저장',
    sort_order      TINYINT UNSIGNED NOT NULL DEFAULT 0
                   COMMENT '한 영상에 상품이 여럿일 때 캡션에 언급된 순서(0부터)',
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_gonggu_video_product_gonggu (gonggu_id),
    KEY idx_gonggu_video_product_name (product_name),
    CONSTRAINT fk_gonggu_video_product_video
        FOREIGN KEY (gonggu_id) REFERENCES gonggu_video (video_id)
        ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='gonggu_video 1건이 홍보하는 상품(1:N) — 가격/옵션/배송비 등 상세 커머스 정보는 별도 테이블(다운스트림 링크 크롤링 결과 저장용)에서 이 테이블을 참조해 관리할 예정';

-- ============================================================
-- 3) gonggu_post — 공구가 "확실한"(최대한 보수적으로 필터링된) 인스타그램 포스트 1건당 1행
-- ============================================================
CREATE TABLE gonggu_post (
    id                  BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    post_id             VARCHAR(50) NOT NULL
                        COMMENT 'instagram_post.post_id와 동일 컬럼명·타입 (ex. 8af8hqQYaE)',
    user_id             VARCHAR(50) NOT NULL
                        COMMENT 'instagram_post.user_id / instagram_user.user_id와 동일 컬럼명·타입. 원본은 nullable이지만 여기서는 필수',
    url                 VARCHAR(300) NULL
                        COMMENT 'instagram_post.url과 동일 컬럼명·타입',
    publish_date        DATETIME NOT NULL
                        COMMENT 'instagram_post.publish_date와 동일 컬럼명·타입 — 캡션의 "오늘/내일" 상대 날짜 표현을 해석할 때 기준점으로 씀',
    gonggu_start_date   DATE NULL
                        COMMENT '공구 시작일. 캡션에 명시적 날짜가 있거나 게시일 기준 상대표현이 명확할 때만 채움 — 특정 불가능하면 NULL(추측/환각 금지)',
    gonggu_end_date     DATE NULL
                        COMMENT '공구 종료일(마감일). 계산 기준은 gonggu_start_date와 동일',
    classification_note VARCHAR(500) NULL
                        COMMENT 'LLM 분류 단계에서 남긴 특이사항 자유서술(500자 이내, 예: "본문엔 상담용 채널톡만 있고 실제 구매는 프로필 링크트리 경유") — 캡션 원문은 instagram_post_description.description을 post_id로 조인해서 볼 것',
    created_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_gonggu_post_post_id (post_id),
    KEY idx_gonggu_post_user (user_id),
    KEY idx_gonggu_post_end_date (gonggu_end_date),
    KEY idx_gonggu_post_publish_date (publish_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='공구가 확실한 것만 보수적으로 필터링한 인스타그램 포스트. 링크 해석 파이프라인의 입력 테이블';

-- ============================================================
-- 4) gonggu_post_product — 한 공구 포스트가 홍보하는 상품(1:N).
--    같은 포스트 안에서도 상품마다 구매 링크 위치/종류가 다를 수 있어 이 테이블에 둔다.
-- ============================================================
CREATE TABLE gonggu_post_product (
    id             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    gonggu_id      VARCHAR(50) NOT NULL
                   COMMENT 'gonggu_post.post_id FK(자연키). gonggu_video_product.gonggu_id(→gonggu_video.video_id)와 동일한 이름·타입으로 통일',
    product_name   VARCHAR(300) NOT NULL
                   COMMENT '상품명(캡션에서 추출한 그대로, 브랜드명 포함 권장)',
    link_location   ENUM('설명_직접링크', '설명_프로필안내', '댓글참여_DM', '고정댓글_더보기', '링크없음_불명') NOT NULL
                   COMMENT '이 상품의 구매 링크가 어디 있는지 — 다운스트림 크롤링 시작점 힌트. "댓글참여_DM"이어도 프로필에 상시 링크(인포크 등)가 따로 있는 경우가 많으니 이 값만으로 크롤링 대상에서 제외하지 말 것',
    url_type        VARCHAR(30) NULL
                   COMMENT '대표 구매 URL 종류 — 네이버_스마트스토어 / 네이버_기타 / 링크모음 / 자사몰_독립몰 / 카카오채널 / 쿠팡_오픈마켓 / 결제플랫폼 / 단축링크 / 기타 / 없음 중 하나(자유 텍스트, 값 늘어날 수 있어 ENUM 대신 VARCHAR)',
    candidate_url   VARCHAR(500) NULL
                   COMMENT '캡션/프로필에서 1차로 발견된 이 상품의 구매 링크 후보(참고용, 미검증) — 실제 최종 링크 확정은 다운스트림 크롤링 단계 책임. 여러 개면 세미콜론(;)으로 구분, 원본부터 "..."로 잘려 있으면 그대로 저장',
    sort_order      TINYINT UNSIGNED NOT NULL DEFAULT 0
                   COMMENT '한 포스트에 상품이 여럿일 때 캡션에 언급된 순서(0부터)',
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_gonggu_post_product_gonggu (gonggu_id),
    KEY idx_gonggu_post_product_name (product_name),
    CONSTRAINT fk_gonggu_post_product_post
        FOREIGN KEY (gonggu_id) REFERENCES gonggu_post (post_id)
        ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='gonggu_post 1건이 홍보하는 상품(1:N) — 가격/옵션/배송비 등 상세 커머스 정보는 별도 테이블(다운스트림 링크 크롤링 결과 저장용)에서 이 테이블을 참조해 관리할 예정';
