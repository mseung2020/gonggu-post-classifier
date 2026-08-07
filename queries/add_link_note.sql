-- ============================================================
-- 링크 판단 이유(link_note)를 상품 테이블에 추가 (2026-08-07).
--
-- 배경: resolve_links(LLM#3)는 상품마다 "왜 done/hold/unresolved/error인지"를 이미
-- note로 만들어 두는데(예: "상품페이지 확인, 상품명 일치" / "상품명이 너무 일반적이라
-- 자동확정 보류" / "후보 링크 없음" / "로그인월 차단 HTTP 4xx"), 지금까진 그 note가
-- data/output/link_resolution.jsonl 파일에만 남고 DB엔 안 들어갔다. runner가 DB로 흘려보낼 때
-- link_status/candidate_url만 복사했기 때문.
--
-- 링크 판단은 본질적으로 상품 단위(각 상품이 독립적으로 done/hold…)라, 그 이유도 상품 행에
-- 건바이건으로 있어야 자연스럽다. 분류 이유(classification_note, LLM#1)가 포스트 단위로
-- 부모에 있는 것과 대칭 — 이쪽은 상품 단위라 상품 테이블에 둔다.
--
-- note는 코드에서 60~160자로 잘려 저장되므로 VARCHAR(255)면 충분하다.
-- 링크와 함께 읽히는 값이라 candidate_url/link_status 뒤에 붙인다.
-- ============================================================

ALTER TABLE gonggu_post_product
  ADD COLUMN link_note VARCHAR(255) NULL AFTER link_status;

ALTER TABLE gonggu_video_product
  ADD COLUMN link_note VARCHAR(255) NULL AFTER link_status;
