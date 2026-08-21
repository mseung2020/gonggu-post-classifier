-- ============================================================
-- 공구기간을 DATE -> DATETIME으로 확장 — 상품 테이블 2개. (2026-08-21)
--
-- 적용 순서: create_gonggu_tables.sql 헤더 목록의 12번.
--
-- 목표: 공구 시작/종료를 날짜뿐 아니라 **시간까지** 담는다. "오늘 20시 오픈" 같은 공구가
-- 실제로 흔한데 날짜 단위로 뭉개면 그날 00:00부터 진행중으로 보여서, 아직 안 열린 공구를
-- 열린 것처럼 노출하게 된다.
--
-- ⚠ 대상은 **상품 테이블**이다. 부모(gonggu_post/gonggu_video)의 기간 컬럼은 2026-08-06에
-- 상품 단위로 이전(create_period_columns.sql -> migrate_period_to_product.sql)된 뒤
-- drop_parent_period_columns.sql로 이미 DROP됐다. 베이스라인 파일(create_gonggu_tables.sql)에
-- 아직 부모 쪽 기간 컬럼이 보이는 것은 그게 "최초 베이스라인"이기 때문이며 살아있는 컬럼이
-- 아니다 — 여기서 건드리지 않는다.
--
-- ⚠ 컬럼명은 그대로 gonggu_start_date / gonggu_end_date다. 타입이 DATETIME이 되었으니
-- _datetime으로 바꾸는 게 이름상 정확하지만, 이 두 컬럼명은 코드(platforms.py의 SQL 빌더,
-- update_gonggu_stage, backfill_period, rescan, case_matrix 뷰, unresolved_board)와 이미
-- 적재된 JSONL 체크포인트 전반에 퍼져 있어 개명 비용이 이득보다 훨씬 크다. "date인데 시간이
-- 들어있다"는 혼란은 아래 COLUMN COMMENT로 막는다.
--
-- ─────────────────────────────────────────────────────────────
-- 값 규칙(비대칭이라 주의 — 코드 쪽 transform._valid_dt와 동일해야 한다)
--   · 시작에 시간 힌트 없음 -> 00:00:00
--   · 종료에 시간 힌트 없음 -> 23:59:59   (그날 자정에 끝나는 게 아니라 그날 "끝까지" 진행)
--   · 시작/종료 어느 쪽이든 실제 시간 힌트가 있으면 그 시간 그대로
--   · 시간만 있고 날짜가 없으면 통째로 NULL (날짜를 추측해서 채우지 않는다)
-- ─────────────────────────────────────────────────────────────
--
-- ⚠⚠ 2번 UPDATE를 반드시 1번 MODIFY와 **같은 작업 단위로 연달아** 실행할 것.
-- MySQL은 DATE -> DATETIME 변환에서 기존 '2026-08-01'을 '2026-08-01 00:00:00'으로 만든다.
-- 시작일은 그게 정확히 맞는 기본값이지만, **종료일은 틀렸다** — 위 규칙대로면 23:59:59여야
-- 한다. MODIFY만 하고 멈추면 "오늘 마감" 공구가 그날 00:00:01부터 전부 '종료'로 뒤집힌다
-- (update_gonggu_stage가 다음 실행에서 그렇게 갱신해버린다). 그래서 2번은 선택이 아니다.
--
-- 2번의 WHERE TIME(...) = '00:00:00' 조건은 (a) 멱등성(다시 돌려도 이미 23:59:59인 행은
-- 대상이 아님)과 (b) 나중에 들어온 "실제 시간 힌트가 있는" 종료일을 덮지 않기 위한 것이다.
-- 다만 마이그레이션 직후 이 시점에는 00:00:00인 종료일이 곧 "시간 힌트 없음"과 동의어이므로
-- (DATE였으니 시간 정보가 애초에 존재할 수 없었다) 정확히 원하는 행만 잡힌다.
-- 반대로 **운영이 새 코드로 한 번이라도 돌아간 뒤에는 2번을 다시 실행하지 말 것** — 그때는
-- 00:00:00이 "자정 종료"라는 진짜 힌트일 수 있다.
-- ============================================================

-- 1) 타입 확장 -----------------------------------------------------------------
ALTER TABLE gonggu_post_product
  MODIFY COLUMN gonggu_start_date DATETIME NULL
      COMMENT '이 상품의 공구 시작 시각. 컬럼명은 _date지만 타입은 DATETIME(2026-08-21 확장). 시간 힌트 없으면 00:00:00. 명시적 날짜/명확한 상대표현만, 없으면 NULL',
  MODIFY COLUMN gonggu_end_date DATETIME NULL
      COMMENT '이 상품의 공구 종료 시각. 시간 힌트 없으면 23:59:59(그날 끝까지 진행하는 것이므로 00:00:00이 아니다). 없으면 NULL';

ALTER TABLE gonggu_video_product
  MODIFY COLUMN gonggu_start_date DATETIME NULL
      COMMENT '이 상품의 공구 시작 시각. 컬럼명은 _date지만 타입은 DATETIME(2026-08-21 확장). 시간 힌트 없으면 00:00:00. 명시적 날짜/명확한 상대표현만, 없으면 NULL',
  MODIFY COLUMN gonggu_end_date DATETIME NULL
      COMMENT '이 상품의 공구 종료 시각. 시간 힌트 없으면 23:59:59(그날 끝까지 진행하는 것이므로 00:00:00이 아니다). 없으면 NULL';

-- 2) 기존 종료일의 00:00:00 -> 23:59:59 보정 (위 ⚠⚠ 참고 — 1번과 함께 반드시 실행) --------
UPDATE gonggu_post_product
   SET gonggu_end_date = DATE_ADD(DATE(gonggu_end_date), INTERVAL 86399 SECOND)
 WHERE gonggu_end_date IS NOT NULL
   AND TIME(gonggu_end_date) = '00:00:00';

UPDATE gonggu_video_product
   SET gonggu_end_date = DATE_ADD(DATE(gonggu_end_date), INTERVAL 86399 SECOND)
 WHERE gonggu_end_date IS NOT NULL
   AND TIME(gonggu_end_date) = '00:00:00';

-- 3) 적용 확인 ----------------------------------------------------------------
-- 아래가 0을 돌려주면(= 00:00:00으로 남은 종료일이 없으면) 2번이 완전히 반영된 것이다.
--   SELECT SUM(TIME(gonggu_end_date) = '00:00:00') AS still_midnight
--     FROM (SELECT gonggu_end_date FROM gonggu_post_product WHERE gonggu_end_date IS NOT NULL
--           UNION ALL
--           SELECT gonggu_end_date FROM gonggu_video_product WHERE gonggu_end_date IS NOT NULL) t;
-- 그리고 stage가 뒤집히지 않았는지 확인하려면 새 코드 배포 전/후로 아래를 비교할 것:
--   SELECT gonggu_stage, COUNT(*) FROM gonggu_post_product GROUP BY gonggu_stage;
