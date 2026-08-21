-- ============================================================
-- 소급 기록본 — link_status / external_url. (기록 작성 2026-08-21)
--
-- ⚠ 이 파일은 "새로 적용할 변경"이 아니다. 두 컬럼은 이미 오래전부터 운영 DB(dev_gongguking)에
-- 존재하고 코드 전체가 이 컬럼을 읽고 쓰지만(load/rescan/backfill/reverify_uc/case_matrix 등),
-- **어떤 SQL 파일에도 DDL이 남아 있지 않았다.** 그래서 queries/를 그대로 실행하는 신규 설치
-- 환경에서는 두 컬럼이 없는 스키마가 만들어져 파이프라인이 즉시 깨진다. 그 구멍을 메우려고
-- 코드에서 실제 사용례를 역추적해 DDL을 복원한 기록이다.
--
-- 따라서:
--   - 기존 운영 DB에서는 실행하지 말 것(이미 존재 → "Duplicate column name" 에러).
--   - 신규 설치에서만, create_gonggu_tables.sql 목록의 3번 위치에서 실행할 것.
--
-- 타입은 운영 DB에서 실측해 확정했다(2026-08-21) — 코드에서 역추적한 추정치와 일치했다:
--     gonggu_post_product.link_status / gonggu_video_product.link_status   varchar(20)
--     gonggu_video.external_url                                            varchar(500)
--
-- 근거(역추적):
--  * link_status — 상품 테이블 2개. 링크 해석 단계의 판정 결과. 실측되는 값은
--      'pending' / 'done' / 'hold' / 'unresolved' / 'error' 그리고 NULL(아직 해석 전).
--    load.py는 03_load_ready에 이 키가 없으면 NULL을 넣으므로 NOT NULL이 아니다.
--    값이 늘어날 수 있어(gonggu_stage와 같은 이유) ENUM 대신 VARCHAR로 둔다.
--    컬럼 위치: add_link_note.sql이 link_note를 "AFTER link_status"로 붙이므로
--    candidate_url 뒤 / link_note 앞이 맞다.
--  * external_url — gonggu_video에만 있다(인스타에는 없는 컬럼: platforms.py 주석 참고).
--    유튜브 채널 '정보'란의 외부 링크로, 캡션에 링크가 없을 때 프로필 대체 경로로 쓴다.
--    load.py가 항상 NULL로 INSERT하고(캡션에 링크가 있으면 채널까지 볼 필요가 없음),
--    tools/crawl_linkbio.py와 tools/unresolved_board.py가 읽는다. 인스타의 대응 컬럼
--    hifen.instagram_user_external_url.external_url과 같은 의미이므로 길이도 그쪽에 맞출 것.
-- ============================================================

ALTER TABLE gonggu_post_product
  ADD COLUMN link_status VARCHAR(20) NULL
      COMMENT '링크 해석 결과(pending/done/hold/unresolved/error, 미해석은 NULL). 값이 늘 수 있어 ENUM 대신 VARCHAR' AFTER candidate_url;

ALTER TABLE gonggu_video_product
  ADD COLUMN link_status VARCHAR(20) NULL
      COMMENT '링크 해석 결과(pending/done/hold/unresolved/error, 미해석은 NULL). 값이 늘 수 있어 ENUM 대신 VARCHAR' AFTER candidate_url;

ALTER TABLE gonggu_video
  ADD COLUMN external_url VARCHAR(500) NULL
      COMMENT '유튜브 채널 정보란의 외부 링크(hifen.instagram_user_external_url.external_url의 유튜브판). 캡션에 구매 링크가 없을 때의 대체 경로 — 캡션에 링크가 있으면 채널을 안 보므로 NULL인 경우가 많다' AFTER video_url;
