-- ============================================================
-- 원문 캡션(description) 컬럼 추가 — 확정 공구 부모 테이블 2개. (2026-08-21)
--
-- 적용 순서: create_gonggu_tables.sql 헤더의 목록 10번. description은 베이스라인이 아니라
-- 이 파일에만 정의된다(컬럼을 베이스라인에 직접 끼워넣지 않는 규약 — 그 파일 헤더 참고).
--
-- 배경: fetch 단계에서 hifen DB에서 이미 캡션 원문을 읽어 LLM#1 입력으로 넘기고 있었지만,
-- 그 원문 자체는 어디에도 저장하지 않고 버려서 다운스트림(상세 보강·검수·재분석)이 원문을
-- 보려면 매번 hifen에 조인해야 했다. 확정 공구(=이 두 테이블에 들어온 건)에 한해 원문을
-- 같이 적재해 자기완결적으로 만든다.
--
-- 컬럼명은 DDL 설계원칙 #2(원본 hifen 대응 컬럼명을 최대한 그대로)에 따라 양쪽 모두
-- description으로 통일한다.
--   - gonggu_post.description  ↔ instagram_post_description.description
--   - gonggu_video.description ↔ YT_video_lists_detail.video_description
--     (유튜브 원본 컬럼명은 video_description이지만, 이미 테이블 자체가 video 전용이라
--      video_ 접두사가 중복이고 두 테이블에서 같은 의미의 컬럼을 다른 이름으로 두면
--      다운스트림 코드가 플랫폼 분기를 더 하게 되므로 description으로 맞춤)
--
-- gonggu_video.title은 이미 존재하고 이미 적재 중이라 여기서 손대지 않는다.
--
-- 타입은 원본 hifen 컬럼 타입을 실측해 그대로 맞췄다(2026-08-21 확인):
--   instagram_post_description.description   varchar(4000)  -> gonggu_post.description
--   YT_video_lists_detail.video_description  longtext       -> gonggu_video.description
-- 두 플랫폼의 타입이 다른 건 원본이 다르기 때문이며, 원본을 따르는 것이 이 스키마의 규약이다.
-- (유튜브가 longtext인 쪽에 varchar를 쓰면 긴 설명이 조용히 잘린다 — load.py는 잘림을 삼키지
--  않으려고 INSERT IGNORE를 일부러 안 쓰지만, 애초에 타입을 맞추는 게 1차 방어선이다.)
--
-- 기존 데이터·컬럼을 건드리지 않는 순수 ADD라 안전(전부 NULL로 생성). 기존 행 소급은
-- gonggu/tools/_backfill_parent_fields.py가 담당한다(별도, 멱등).
-- ============================================================

ALTER TABLE gonggu_post
  ADD COLUMN description VARCHAR(4000) NULL
      COMMENT 'instagram_post_description.description 원문 그대로(LLM#1에 넘긴 그 캡션). 확정 공구만 적재' AFTER url;

ALTER TABLE gonggu_video
  ADD COLUMN description LONGTEXT NULL
      COMMENT 'YT_video_lists_detail.video_description(또는 brand.video_description) 원문 그대로. 제목은 title 컬럼에 별도 보관하므로 여기엔 제목을 붙이지 않는다' AFTER title;
