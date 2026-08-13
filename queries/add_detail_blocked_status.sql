-- ============================================================
-- detail_status ENUM에 'blocked' 추가 (2026-08-12).
--
-- 배경: 상세 수집(enrich_detail)을 운영 성격이 정반대인 두 패스로 가른다.
--   fast 패스 — 무인·병렬·안정(requests→Playwright). 자사몰(카페24 등)을 대량 처리.
--   uc   패스 — 사람이 곁에서·직렬·낮은 동시성(undetected_chromedriver). 네이버/오픈마켓처럼
--               로그인월·봇확인으로 막히는 곳만 구제.
-- 두 패스는 순서 barrier 없이 DB 상태를 체크포인트로 느슨하게 결합된다. 그 경계를 나타내는
-- 상태가 'blocked'다: fast 패스가 안티봇 차단을 만난 상품을 'blocked'로 남기면, uc 패스가
-- 호스트와 무관하게 'blocked'인 것만 골라 uc로 다시 시도한다(모르는/새 차단 호스트까지 자동
-- 커버 — 호스트 화이트리스트를 손으로 관리할 필요가 없음).
--
--   blocked = fast(무인) 경로에서 안티봇/로그인월/봇확인에 막힘 → uc 패스가 처리할 대상.
--             error(일시 실패, fast가 재시도)와 구분: blocked는 fast가 재시도하지 않는다
--             (다시 막힐 뿐이므로). done/gone과 함께 fast 대상에서 빠지고 uc 대상에만 잡힌다.
--
-- 기존 테이블에 적용(신규 설치는 create_detail_tables.sql이 이미 blocked 포함):
--   mysql -h <host> -u <user> -p <db> < queries/add_detail_blocked_status.sql
-- 기존 데이터/값은 그대로 두고 ENUM 허용값만 넓히는 변경이라 안전하다(idempotent — 여러 번
-- 실행해도 결과 동일).
-- ============================================================

ALTER TABLE gonggu_post_product_detail  MODIFY COLUMN detail_status
    ENUM('pending', 'done', 'error', 'gone', 'blocked') NOT NULL DEFAULT 'pending'
    COMMENT '처리 상태. error=일시 실패(fast 재시도), gone=페이지 영구 소멸(재시도 안 함), blocked=fast(무인) 경로 차단 → uc 패스가 처리';

ALTER TABLE gonggu_video_product_detail MODIFY COLUMN detail_status
    ENUM('pending', 'done', 'error', 'gone', 'blocked') NOT NULL DEFAULT 'pending'
    COMMENT '처리 상태. error=일시 실패(fast 재시도), gone=페이지 영구 소멸(재시도 안 함), blocked=fast(무인) 경로 차단 → uc 패스가 처리';
