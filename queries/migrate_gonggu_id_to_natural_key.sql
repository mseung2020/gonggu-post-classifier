-- gonggu_id를 서로게이트 id(BIGINT) 참조에서 자연키(post_id/video_id, VARCHAR(50)) 참조로
-- 바꾸는 마이그레이션. 이미 데이터가 들어간 dev_gongguking에 안전하게 적용하기 위해
-- DROP 없이 ALTER + 백필(backfill)로 진행한다. create_gonggu_tables.sql은 이미 이 구조로
-- 갱신돼 있음(신규 설치 기준) — 이 파일은 "이미 만들어서 데이터 넣은 DB"를 위한 것.
--
-- 실행 전 백업 권장: mysqldump -h ... dev_gongguking gonggu_post gonggu_post_product
--                     gonggu_video gonggu_video_product > backup.sql

-- ============================================================
-- 1) gonggu_post_product.gonggu_id: id(BIGINT) -> post_id(VARCHAR(50))
-- ============================================================
ALTER TABLE gonggu_post_product DROP FOREIGN KEY fk_gonggu_post_product_post;

ALTER TABLE gonggu_post_product ADD COLUMN gonggu_id_new VARCHAR(50) NULL AFTER gonggu_id;

UPDATE gonggu_post_product pp
JOIN gonggu_post p ON p.id = pp.gonggu_id
SET pp.gonggu_id_new = p.post_id;

-- 백필 후 NULL이 남아있으면(고아 행) 여기서 멈춰서 확인할 것.
SELECT COUNT(*) AS orphan_post_products FROM gonggu_post_product WHERE gonggu_id_new IS NULL;

ALTER TABLE gonggu_post_product DROP COLUMN gonggu_id;
ALTER TABLE gonggu_post_product CHANGE COLUMN gonggu_id_new gonggu_id VARCHAR(50) NOT NULL
    COMMENT 'gonggu_post.post_id FK(자연키). gonggu_video_product.gonggu_id(→gonggu_video.video_id)와 동일한 이름·타입으로 통일';

ALTER TABLE gonggu_post_product ADD KEY idx_gonggu_post_product_gonggu (gonggu_id);
ALTER TABLE gonggu_post_product
    ADD CONSTRAINT fk_gonggu_post_product_post
        FOREIGN KEY (gonggu_id) REFERENCES gonggu_post (post_id)
        ON DELETE CASCADE ON UPDATE CASCADE;

-- ============================================================
-- 2) gonggu_video_product.gonggu_id: id(BIGINT) -> video_id(VARCHAR(50))
-- ============================================================
ALTER TABLE gonggu_video_product DROP FOREIGN KEY fk_gonggu_video_product_video;

ALTER TABLE gonggu_video_product ADD COLUMN gonggu_id_new VARCHAR(50) NULL AFTER gonggu_id;

UPDATE gonggu_video_product vp
JOIN gonggu_video v ON v.id = vp.gonggu_id
SET vp.gonggu_id_new = v.video_id;

SELECT COUNT(*) AS orphan_video_products FROM gonggu_video_product WHERE gonggu_id_new IS NULL;

ALTER TABLE gonggu_video_product DROP COLUMN gonggu_id;
ALTER TABLE gonggu_video_product CHANGE COLUMN gonggu_id_new gonggu_id VARCHAR(50) NOT NULL
    COMMENT 'gonggu_video.video_id FK(자연키). gonggu_post_product.gonggu_id(→gonggu_post.post_id)와 동일한 이름·타입으로 통일';

ALTER TABLE gonggu_video_product ADD KEY idx_gonggu_video_product_gonggu (gonggu_id);
ALTER TABLE gonggu_video_product
    ADD CONSTRAINT fk_gonggu_video_product_video
        FOREIGN KEY (gonggu_id) REFERENCES gonggu_video (video_id)
        ON DELETE CASCADE ON UPDATE CASCADE;

-- 마이그레이션 후 확인
SELECT pp.gonggu_id, p.post_id, pp.product_name
FROM gonggu_post_product pp JOIN gonggu_post p ON p.post_id = pp.gonggu_id
LIMIT 5;

-- ============================================================
-- 3) 후속 변경(직접 실행됨, 참고용) — 컬럼명을 공통 이름 gonggu_id에서 각 부모의 자연키와
--    똑같은 이름(post_id/video_id)으로 한 번 더 변경. 위 1)/2) 마이그레이션 실행 후에 적용됨.
--    create_gonggu_tables.sql은 이미 이 최종 상태로 갱신돼 있음. 참고로 남겨두는 것뿐이라
--    이 파일을 처음부터 실행하는 경우라면 아래도 순서대로 같이 실행하면 최종 상태가 됨.
-- ============================================================
-- ALTER TABLE gonggu_post_product DROP FOREIGN KEY fk_gonggu_post_product_post;
-- ALTER TABLE gonggu_post_product CHANGE COLUMN gonggu_id post_id VARCHAR(50) NOT NULL
--     COMMENT 'gonggu_post.post_id FK(자연키)';
-- ALTER TABLE gonggu_post_product
--     ADD CONSTRAINT fk_gonggu_post_product_post
--         FOREIGN KEY (post_id) REFERENCES gonggu_post (post_id)
--         ON DELETE CASCADE ON UPDATE CASCADE;
--
-- ALTER TABLE gonggu_video_product DROP FOREIGN KEY fk_gonggu_video_product_video;
-- ALTER TABLE gonggu_video_product CHANGE COLUMN gonggu_id video_id VARCHAR(50) NOT NULL
--     COMMENT 'gonggu_video.video_id FK(자연키)';
-- ALTER TABLE gonggu_video_product
--     ADD CONSTRAINT fk_gonggu_video_product_video
--         FOREIGN KEY (video_id) REFERENCES gonggu_video (video_id)
--         ON DELETE CASCADE ON UPDATE CASCADE;
