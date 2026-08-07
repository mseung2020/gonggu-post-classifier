-- ============================================================
-- 전반부 대공사(기간→상품 이전) 마지막 단계: parent 기간/스테이지 컬럼 제거.
--
-- ⚠ 실행 시점: 아래가 모두 확인된 뒤에만 실행할 것 (되돌리기 어려운 컬럼/데이터 삭제).
--   1) 마이그레이션(migrate_period_to_product.sql, 단일상품)과
--      다중상품 백필(python3 -m gonggu._migrate_multiproduct_periods)로 product에 기간이 채워짐
--   2) daily를 한 바퀴 정상 실행해 신규 데이터가 product 기간/stage로 잘 적재됨을 확인
--   3) 코드 전부 product 기준으로 전환됨(transform/update_gonggu_stage/rescan_inprogress/
--      backfill_period/load/enrich_detail) — 이 커밋 기준 완료
--
-- 이 시점 이후 parent(gonggu_post/gonggu_video)의 기간/스테이지는 아무도 읽지 않는다
-- (정본은 product). is_calendar_feed는 parent에 남긴다(포스트 전체 속성).
-- ============================================================

ALTER TABLE gonggu_post
  DROP COLUMN gonggu_start_date,
  DROP COLUMN gonggu_end_date,
  DROP COLUMN gonggu_stage;

ALTER TABLE gonggu_video
  DROP COLUMN gonggu_start_date,
  DROP COLUMN gonggu_end_date,
  DROP COLUMN gonggu_stage;
