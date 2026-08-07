-- ============================================================
-- 전반부 대공사(기간→상품 이전) 2단계: parent 기간/스테이지 → product 복사
--
-- 배경: 공구기간(gonggu_start_date/end_date)과 gonggu_stage를 포스트(parent) 단위에서
-- 상품(product) 단위로 완전 이전한다(달력 피드처럼 한 게시물에 여러 공구가 각기 다른
-- 기간을 갖는 경우를 정확히 표현하기 위함). parent 컬럼은 모든 코드가 product를 읽도록
-- 바뀐 뒤 맨 마지막 단계에서 DROP하므로, 그 전에 기존 적재분의 기간을 product로 옮겨 보존한다.
--
-- ⚠ 핵심: **상품이 정확히 1개인 포스트만 복사한다.** 다중상품 포스트는 parent에 기간이
-- 1건뿐이라 그걸 모든 상품에 복사하면 "상품별로 다른 실제 기간"을 하나의 값으로 뭉개버린다
-- (달력 피드가 대표 사례). 그래서 다중상품 상품은 gonggu_stage=NULL로 남겨두고, LLM#1
-- 프롬프트를 상품별 기간으로 개편(3단계)한 뒤 그 포스트들만 재분류해서 정확히 채운다.
-- 단일상품 포스트(전체의 ~98%)는 parent=product가 확실하므로 이 복사만으로 정확하다.
--
-- 실행 전제: 1단계 DDL(product에 기간/stage 컬럼 추가) 적용됨.
-- 재실행 안전: gonggu_stage IS NULL인 상품만 갱신 → 여러 번 돌려도 무해.
-- ============================================================

-- 1) 인스타 — 상품이 정확히 1개인 포스트만
UPDATE gonggu_post_product pp
JOIN gonggu_post p ON p.post_id = pp.post_id
JOIN (SELECT post_id FROM gonggu_post_product GROUP BY post_id HAVING COUNT(*) = 1) s
     ON s.post_id = pp.post_id
SET pp.gonggu_start_date = p.gonggu_start_date,
    pp.gonggu_end_date   = p.gonggu_end_date,
    pp.gonggu_stage      = p.gonggu_stage
WHERE pp.gonggu_stage IS NULL;

-- 2) 유튜브 — 상품이 정확히 1개인 영상만
UPDATE gonggu_video_product pp
JOIN gonggu_video p ON p.video_id = pp.video_id
JOIN (SELECT video_id FROM gonggu_video_product GROUP BY video_id HAVING COUNT(*) = 1) s
     ON s.video_id = pp.video_id
SET pp.gonggu_start_date = p.gonggu_start_date,
    pp.gonggu_end_date   = p.gonggu_end_date,
    pp.gonggu_stage      = p.gonggu_stage
WHERE pp.gonggu_stage IS NULL;

-- 확인: filled_single = 단일상품 수, null_multi = 다중상품 수(재분류로 채울 대상)
-- SELECT COUNT(*) total, SUM(gonggu_stage IS NOT NULL) filled_single,
--        SUM(gonggu_stage IS NULL) null_multi FROM gonggu_post_product;
-- SELECT COUNT(*) total, SUM(gonggu_stage IS NOT NULL) filled_single,
--        SUM(gonggu_stage IS NULL) null_multi FROM gonggu_video_product;
