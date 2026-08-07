-- ============================================================
-- 전반부 대공사(기간→상품 이전) 1단계 DDL: 상품 테이블에 공구기간/스테이지 컬럼 추가,
-- 포스트 테이블에 달력 피드 플래그 추가. (적용 완료본 기록 — 2026-08-06)
--
-- 배경: 공구기간(gonggu_start_date/end_date)과 gonggu_stage를 포스트(parent) 단위에서
-- 상품(product) 단위로 완전 이전한다. 예고 달력처럼 한 게시물에 여러 공구가 각기 다른
-- 기간을 갖는 경우를 정확히 표현하기 위함. parent의 기존 기간/스테이지 컬럼은 모든 코드가
-- product를 읽도록 전환된 뒤 마지막 단계에서 DROP한다(drop_parent_period_columns.sql, 별도).
--
-- 기존 데이터·컬럼을 건드리지 않는 순수 ADD라 안전(전부 NULL 또는 기본값 0으로 생성).
-- ============================================================

-- 상품 테이블: 상품별 공구기간/스테이지 (정본)
ALTER TABLE gonggu_post_product
  ADD COLUMN gonggu_start_date DATE NULL
      COMMENT '이 상품의 공구 시작일. 상품별 관리. 명시적 날짜/명확한 상대표현만, 없으면 NULL' AFTER sort_order,
  ADD COLUMN gonggu_end_date DATE NULL
      COMMENT '이 상품의 공구 종료일. 계산 기준은 시작일과 동일' AFTER gonggu_start_date,
  ADD COLUMN gonggu_stage VARCHAR(20) NULL
      COMMENT '이 상품의 공구 상태(시작전/진행중/종료/판단불가). 상품 기간을 오늘과 비교해 계산' AFTER gonggu_end_date;

ALTER TABLE gonggu_video_product
  ADD COLUMN gonggu_start_date DATE NULL
      COMMENT '이 상품의 공구 시작일. 상품별 관리. 명시적 날짜/명확한 상대표현만, 없으면 NULL' AFTER sort_order,
  ADD COLUMN gonggu_end_date DATE NULL
      COMMENT '이 상품의 공구 종료일. 계산 기준은 시작일과 동일' AFTER gonggu_start_date,
  ADD COLUMN gonggu_stage VARCHAR(20) NULL
      COMMENT '이 상품의 공구 상태(시작전/진행중/종료/판단불가)' AFTER gonggu_end_date;

-- 포스트/영상 테이블: 달력 피드 플래그
ALTER TABLE gonggu_post
  ADD COLUMN is_calendar_feed TINYINT(1) NOT NULL DEFAULT 0
      COMMENT '이 포스트가 개별 공구가 아니라 여러 공구를 나열한 예고 달력/일정 안내 피드인지(0/1). LLM#1이 판정';

ALTER TABLE gonggu_video
  ADD COLUMN is_calendar_feed TINYINT(1) NOT NULL DEFAULT 0
      COMMENT '이 영상이 개별 공구가 아니라 여러 공구를 나열한 예고 달력/일정 안내 피드인지(0/1). LLM#1이 판정';
