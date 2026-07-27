-- ⚠ 위험: 이 파일은 gonggu_video/gonggu_post와 그 상품 테이블을 전부 DROP한다.
-- 실행 전 반드시 백업할 것:
--   mysqldump -h ... dev_gongguking gonggu_post gonggu_post_product \
--             gonggu_video gonggu_video_product > backup.sql
--
-- create_gonggu_tables.sql에는 DROP이 없다(신규 설치 시 실수로 기존 데이터를 날리지 않도록
-- 분리해둠) — 스키마를 완전히 새로 만들고 싶을 때만 이 파일을 먼저 실행한 뒤
-- create_gonggu_tables.sql을 실행할 것.
--
-- 자식(FK 있는 쪽) 먼저 DROP.
DROP TABLE IF EXISTS gonggu_product;
DROP TABLE IF EXISTS gonggu_video_product;
DROP TABLE IF EXISTS gonggu_post_product;
DROP TABLE IF EXISTS gonggu_video;
DROP TABLE IF EXISTS gonggu_post;
