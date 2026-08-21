-- ============================================================
-- 크리에이터 이름 컬럼 추가 — gonggu_post.username / gonggu_video.channel_name. (2026-08-21)
--
-- 적용 순서: create_gonggu_tables.sql 헤더 목록의 11번. 컬럼을 베이스라인에 직접 끼워넣지
-- 않는 규약에 따라 이 파일에만 정의된다.
--
-- 배경: description(원문 캡션)과 같은 목적 — 다운스트림(공구킹 서비스)이 hifen에 조인하지 않고
-- 이 테이블만 보고 "누가 올린 공구인지"까지 알 수 있게 자기완결적으로 만든다. user_id/channel_id는
-- 이미 있지만 사람이 읽을 수 있는 이름이 없어서 화면에 뿌리려면 매번 hifen을 봐야 했다.
--
-- ⚠ 이 두 값은 description과 성질이 다르다 — description은 게시물에 고정된 값이지만
-- 계정 이름/채널명은 **나중에 바뀔 수 있다.** 여기 저장하는 값은 "수집 시점의 이름" 스냅샷이며,
-- 현재 이름이 반드시 필요하면 user_id/channel_id로 hifen을 조인해야 한다. 이 구분을 컬럼
-- COMMENT에도 박아둔다 — 나중에 "왜 hifen과 값이 다르냐"는 의문이 반드시 생기기 때문이다.
--
-- 원본 컬럼 / 타입(2026-08-21 실측):
--   instagram_user.username   varchar(300)  -> gonggu_post.username     (post.user_id로 조인)
--   youtuber_info.title       varchar(200)  -> gonggu_video.channel_name (channel_id로 조인)
--
-- 컬럼명이 원본과 다른 유일한 곳: youtuber_info의 컬럼명은 title(코멘트 '채널 제목')이지만,
-- gonggu_video.title은 이미 **영상** 제목을 담고 있어 그 이름을 쓸 수 없다. 그래서 원본
-- 컬럼명을 따르는 원칙(create_gonggu_tables.sql 설계원칙 #2)에서 의도적으로 벗어나
-- channel_name으로 둔다. 인스타는 원본 컬럼명 username을 그대로 쓴다.
--
-- 조인 안전성(2026-08-21 실측):
--   - collation: instagram_post.user_id / instagram_user.user_id 모두 utf8mb4_unicode_ci로
--     동일하고, YT_video_lists.channel_id / brand.channel_id / youtuber_info.channel_id
--     셋 다 utf8mb4_unicode_ci다. 따라서 이번 조인에는 COLLATE 힌트가 필요 없다.
--     (fetch_source.py가 instagram_user_external_url 조인에만 COLLATE를 붙여둔 것은 그
--      테이블만 utf8mb4_0900_ai_ci를 쓰기 때문 — 이번 조인과는 무관한 별개 사유다.)
--   - 커버리지: youtuber_info는 PK가 channel_id인 505,735행 테이블이고, 현재 gonggu_video의
--     고유 channel_id 1,363개가 1,363/1,363 전부 매칭된다. 그래서 키워드 경로(YT_video_lists
--     기반)와 PPL 경로(brand 기반) 어느 쪽으로 들어온 영상이든 같은 조인으로 채워진다
--     — brand.channel_title을 쓰면 PPL 경로만 채워지는 절반짜리 컬럼이 됐을 것이다.
--
-- 기존 데이터·컬럼을 건드리지 않는 순수 ADD라 안전(전부 NULL로 생성). 기존 행 소급은
-- gonggu/tools/_backfill_parent_fields.py가 description과 함께 한 번에 처리한다.
-- ============================================================

ALTER TABLE gonggu_post
  ADD COLUMN username VARCHAR(300) NULL
      COMMENT 'instagram_user.username과 동일 컬럼명·타입 — user_id로 조인해 가져온 계정 핸들. 수집 시점 스냅샷이라 이후 변경되었을 수 있음(현재 값이 필요하면 user_id로 hifen 조인)' AFTER user_id;

ALTER TABLE gonggu_video
  ADD COLUMN channel_name VARCHAR(200) NULL
      COMMENT 'youtuber_info.title(채널 제목)과 동일 타입 — channel_id로 조인해 가져온 채널명. 컬럼명이 원본(title)과 다른 이유는 이 테이블의 title이 이미 영상 제목이기 때문. 수집 시점 스냅샷이라 이후 변경되었을 수 있음' AFTER channel_id;
