-- gonggu/case_matrix.py --emit-sql 로 생성됨(직접 손으로 고치지 말 것).
-- 읽기 전용 뷰 1개(v_gonggu_case_axes) + 예제 집계 쿼리.
-- 뷰만 만들고 기존 테이블/데이터는 전혀 건드리지 않는다.

CREATE OR REPLACE VIEW v_gonggu_case_axes AS
SELECT f.*,
      /* --- 축(라벨) --- */
      CASE WHEN f.sd IS NOT NULL AND f.ed IS NOT NULL THEN 'A_시작O_종료O'
           WHEN f.sd IS NOT NULL AND f.ed IS     NULL THEN 'B_시작O_종료X'
           WHEN f.sd IS     NULL AND f.ed IS NOT NULL THEN 'C_시작X_종료O'
           ELSE                                            'D_시작X_종료X' END AS c_period,
      COALESCE(f.stage, '(NULL)')                                 AS c_stage,
      COALESCE(f.link_location, '(NULL)')                         AS c_loc,
      COALESCE(NULLIF(f.url_type, ''), '(NULL)')                  AS c_urltype,
      CASE
      WHEN (f.candidate_url IS NULL OR f.candidate_url = '')                       THEN '00_URL없음'
      WHEN (f.candidate_url LIKE '%nid.naver.com%' OR f.candidate_url LIKE '%accounts.kakao.com%' OR f.candidate_url LIKE '%account.kakao.com%' OR f.candidate_url LIKE '%mkt.shopping.naver%')    THEN '01_로그인월·차단도메인'
      WHEN (f.candidate_url LIKE '%litt.ly%' OR f.candidate_url LIKE '%lit.link%' OR f.candidate_url LIKE '%inpock%' OR f.candidate_url LIKE '%inpk.link%' OR f.candidate_url LIKE '%linktr.ee%' OR f.candidate_url LIKE '%//tr.ee%' OR f.candidate_url LIKE '%hity.io%' OR f.candidate_url LIKE '%instabio.cc%' OR f.candidate_url LIKE '%bio.site%' OR f.candidate_url LIKE '%linkon.id%' OR f.candidate_url LIKE '%linkseller.net%' OR f.candidate_url LIKE '%taplink%' OR f.candidate_url LIKE '%linkby.me%')     THEN '02_링크모음허브(인포크·litt.ly류)'
      WHEN (f.candidate_url LIKE '%/error%' OR f.candidate_url LIKE '%/notfound%' OR f.candidate_url LIKE '%/not-found%' OR f.candidate_url LIKE '%/404%')  THEN '03_깨진경로(error·404)'
      WHEN (f.candidate_url LIKE '%docs.google.com/forms%' OR f.candidate_url LIKE '%forms.gle%' OR f.candidate_url LIKE '%form.naver.com%' OR f.candidate_url LIKE '%naver.me/form%' OR f.candidate_url LIKE '%forms.office.com%' OR f.candidate_url LIKE '%tally.so%' OR f.candidate_url LIKE '%walla.my%')    THEN '04_폼·설문'
      WHEN (f.candidate_url LIKE '%smartstore.naver.com%' OR f.candidate_url LIKE '%brand.naver.com%')  THEN '05_네이버_스마트·브랜드스토어'
      WHEN (f.candidate_url LIKE '%shopping.naver.com%')   THEN '06_네이버_쇼핑'
      WHEN (f.candidate_url LIKE '%blog.naver.com%' OR f.candidate_url LIKE '%cafe.naver.com%')   THEN '07_네이버_블로그·카페(몰아님)'
      WHEN (f.candidate_url LIKE '%naver.me%' OR f.candidate_url LIKE '%bit.ly%' OR f.candidate_url LIKE '%srok.kr%' OR f.candidate_url LIKE '%me2.do%' OR f.candidate_url LIKE '%vo.la%' OR f.candidate_url LIKE '%buly.kr%' OR f.candidate_url LIKE '%link.coupang.com%')   THEN '08_단축링크'
      WHEN (f.candidate_url LIKE '%pf.kakao.com%' OR f.candidate_url LIKE '%open.kakao.com%' OR f.candidate_url LIKE '%kakao.com%')   THEN '09_카카오채널'
      WHEN (f.candidate_url LIKE '%coupang.com%' OR f.candidate_url LIKE '%gmarket.co.kr%' OR f.candidate_url LIKE '%auction.co.kr%' OR f.candidate_url LIKE '%11st.co.kr%' OR f.candidate_url LIKE '%interpark.com%' OR f.candidate_url LIKE '%tmon.co.kr%' OR f.candidate_url LIKE '%wemakeprice.com%' OR f.candidate_url LIKE '%lotteon.com%' OR f.candidate_url LIKE '%ssg.com%' OR f.candidate_url LIKE '%oliveyoung.co.kr%' OR f.candidate_url LIKE '%kurly.com%' OR f.candidate_url LIKE '%musinsa.com%' OR f.candidate_url LIKE '%ably%' OR f.candidate_url LIKE '%zigzag%')  THEN '10_오픈마켓·대형몰'
      WHEN (f.candidate_url LIKE '%srookpay%' OR f.candidate_url LIKE '%payapp%' OR f.candidate_url LIKE '%tosspayments%' OR f.candidate_url LIKE '%kakaopay%' OR f.candidate_url LIKE '%naverpay%' OR f.candidate_url LIKE '%smartro%')     THEN '11_결제플랫폼'
      WHEN (f.candidate_url LIKE '%cafe24.com%' OR f.candidate_url LIKE '%imweb.me%' OR f.candidate_url LIKE '%shopby%' OR f.candidate_url LIKE '%sixshop%' OR f.candidate_url LIKE '%godomall%' OR f.candidate_url LIKE '%makeshop%' OR f.candidate_url LIKE '%wixsite%' OR f.candidate_url LIKE '%myshopify.com%') THEN '12_자사몰(빌더도메인)'
      WHEN f.candidate_url LIKE '%naver.com%'        THEN '13_네이버_기타'
      ELSE '14_기타·독립도메인'
    END                                                  AS c_urlkind,
      CASE
      WHEN (f.candidate_url IS NULL OR f.candidate_url = '') THEN 'N/A_URL없음'
      WHEN (LOWER(f.candidate_url) LIKE '%/products/%' OR LOWER(f.candidate_url) LIKE '%/product/%' OR LOWER(f.candidate_url) LIKE '%/goods%' OR LOWER(f.candidate_url) LIKE '%/item%' OR LOWER(f.candidate_url) LIKE '%prod_no%' OR LOWER(f.candidate_url) LIKE '%productno%' OR LOWER(f.candidate_url) LIKE '%goodsno%' OR LOWER(f.candidate_url) LIKE '%/vp/products/%' OR LOWER(f.candidate_url) LIKE '%/detail%' OR LOWER(f.candidate_url) LIKE '%product_no%' OR LOWER(f.candidate_url) LIKE '%/dp/%') THEN '상품상세_추정'
      ELSE '메인·목록·기획전_추정'
    END                                                     AS c_depth,
      COALESCE(f.link_status, '(NULL_미해석)')                     AS c_linkstatus,
      COALESCE(f.detail_status, '(행없음)')                        AS c_detail,
      CASE
      WHEN f.link_status = 'done' THEN 'R_해결됨(done)'
      WHEN (f.candidate_url IS NULL OR f.candidate_url = '') AND f.link_location = '링크없음_불명'                     THEN 'U01_링크자체없음(불명)'
      WHEN (f.candidate_url IS NULL OR f.candidate_url = '') AND f.link_location = '댓글참여_DM'                       THEN 'U02_댓글·DM유도_URL없음'
      WHEN (f.candidate_url IS NULL OR f.candidate_url = '') AND f.link_location = '고정댓글_더보기'                   THEN 'U03_고정댓글안내_URL없음'
      WHEN (f.candidate_url IS NULL OR f.candidate_url = '') AND f.link_location = '설명_프로필안내'                   THEN 'U04_프로필안내인데_URL없음'
      WHEN (f.candidate_url IS NULL OR f.candidate_url = '')                                                          THEN 'U05_직접링크라는데_URL없음'
      WHEN (f.candidate_url LIKE '%nid.naver.com%' OR f.candidate_url LIKE '%accounts.kakao.com%' OR f.candidate_url LIKE '%account.kakao.com%' OR f.candidate_url LIKE '%mkt.shopping.naver%')                               THEN 'U06_로그인월·차단에서멈춤'
      WHEN (f.candidate_url LIKE '%litt.ly%' OR f.candidate_url LIKE '%lit.link%' OR f.candidate_url LIKE '%inpock%' OR f.candidate_url LIKE '%inpk.link%' OR f.candidate_url LIKE '%linktr.ee%' OR f.candidate_url LIKE '%//tr.ee%' OR f.candidate_url LIKE '%hity.io%' OR f.candidate_url LIKE '%instabio.cc%' OR f.candidate_url LIKE '%bio.site%' OR f.candidate_url LIKE '%linkon.id%' OR f.candidate_url LIKE '%linkseller.net%' OR f.candidate_url LIKE '%taplink%' OR f.candidate_url LIKE '%linkby.me%')                                THEN 'U07_허브까지만(제품링크미확정)'
      WHEN (f.candidate_url LIKE '%/error%' OR f.candidate_url LIKE '%/notfound%' OR f.candidate_url LIKE '%/not-found%' OR f.candidate_url LIKE '%/404%')                             THEN 'U08_깨진경로'
      WHEN (f.candidate_url LIKE '%docs.google.com/forms%' OR f.candidate_url LIKE '%forms.gle%' OR f.candidate_url LIKE '%form.naver.com%' OR f.candidate_url LIKE '%naver.me/form%' OR f.candidate_url LIKE '%forms.office.com%' OR f.candidate_url LIKE '%tally.so%' OR f.candidate_url LIKE '%walla.my%')                               THEN 'U09_폼·설문뿐'
      WHEN (f.candidate_url LIKE '%blog.naver.com%' OR f.candidate_url LIKE '%cafe.naver.com%')                              THEN 'U10_블로그·카페뿐'
      WHEN (f.candidate_url LIKE '%naver.me%' OR f.candidate_url LIKE '%bit.ly%' OR f.candidate_url LIKE '%srok.kr%' OR f.candidate_url LIKE '%me2.do%' OR f.candidate_url LIKE '%vo.la%' OR f.candidate_url LIKE '%buly.kr%' OR f.candidate_url LIKE '%link.coupang.com%')                              THEN 'U11_단축링크_미해석'
      WHEN (f.candidate_url LIKE '%pf.kakao.com%' OR f.candidate_url LIKE '%open.kakao.com%' OR f.candidate_url LIKE '%kakao.com%')                              THEN 'U12_카카오채널뿐'
      WHEN (LOWER(f.candidate_url) LIKE '%/products/%' OR LOWER(f.candidate_url) LIKE '%/product/%' OR LOWER(f.candidate_url) LIKE '%/goods%' OR LOWER(f.candidate_url) LIKE '%/item%' OR LOWER(f.candidate_url) LIKE '%prod_no%' OR LOWER(f.candidate_url) LIKE '%productno%' OR LOWER(f.candidate_url) LIKE '%goodsno%' OR LOWER(f.candidate_url) LIKE '%/vp/products/%' OR LOWER(f.candidate_url) LIKE '%/detail%' OR LOWER(f.candidate_url) LIKE '%product_no%' OR LOWER(f.candidate_url) LIKE '%/dp/%')                        THEN 'U13_상품상세로보이는데_미확정'
      ELSE 'U14_몰·기타도메인_메인·목록에서멈춤'
    END                                                     AS c_unres,
      CASE WHEN f.sib_n > 1 THEN '다상품(2+)' ELSE '단일상품' END   AS c_multi,
      CASE WHEN f.platform = 'ig' THEN 'N/A_인스타'
           WHEN f.external_url IS NULL OR f.external_url = '' THEN '채널링크없음'
           ELSE '채널링크있음' END                                 AS c_ext,
      /* --- O/X 플래그 (1=O, 0=X) --- */
      CASE WHEN f.sd IS NOT NULL THEN 1 ELSE 0 END                                    AS ox_start,
      CASE WHEN f.ed IS NOT NULL THEN 1 ELSE 0 END                                    AS ox_end,
      CASE WHEN f.cnote IS NOT NULL AND f.cnote <> '' THEN 1 ELSE 0 END               AS ox_note,
      CASE WHEN (f.candidate_url IS NULL OR f.candidate_url = '') THEN 0 ELSE 1 END                                         AS ox_url,
      CASE WHEN f.link_status = 'done' THEN 1 ELSE 0 END                              AS ox_linkdone,
      CASE WHEN (f.candidate_url LIKE '%smartstore.naver.com%' OR f.candidate_url LIKE '%brand.naver.com%' OR f.candidate_url LIKE '%shopping.naver.com%') THEN 1 ELSE 0 END  AS ox_naver,
      CASE WHEN (f.candidate_url LIKE '%litt.ly%' OR f.candidate_url LIKE '%lit.link%' OR f.candidate_url LIKE '%inpock%' OR f.candidate_url LIKE '%inpk.link%' OR f.candidate_url LIKE '%linktr.ee%' OR f.candidate_url LIKE '%//tr.ee%' OR f.candidate_url LIKE '%hity.io%' OR f.candidate_url LIKE '%instabio.cc%' OR f.candidate_url LIKE '%bio.site%' OR f.candidate_url LIKE '%linkon.id%' OR f.candidate_url LIKE '%linkseller.net%' OR f.candidate_url LIKE '%taplink%' OR f.candidate_url LIKE '%linkby.me%') THEN 1 ELSE 0 END               AS ox_hub,
      CASE WHEN f.detail_status IS NOT NULL THEN 1 ELSE 0 END                         AS ox_detailrow,
      CASE WHEN f.detail_status = 'done' THEN 1 ELSE 0 END                            AS ox_detaildone,
      CASE WHEN f.sale_price IS NOT NULL THEN 1 ELSE 0 END                            AS ox_price,
      CASE WHEN f.original_price IS NOT NULL THEN 1 ELSE 0 END                        AS ox_origprice,
      CASE WHEN f.discount_rate IS NOT NULL OR f.discount_amount IS NOT NULL
                THEN 1 ELSE 0 END                                                     AS ox_discount,
      CASE WHEN f.brand_name_kr IS NOT NULL OR f.brand_name_en IS NOT NULL
                THEN 1 ELSE 0 END                                                     AS ox_brand,
      CASE WHEN f.category IS NOT NULL THEN 1 ELSE 0 END                              AS ox_category,
      CASE WHEN f.search_keywords IS NOT NULL THEN 1 ELSE 0 END                       AS ox_keywords,
      CASE WHEN f.free_shipping IS NOT NULL OR f.shipping_fee IS NOT NULL
                OR (f.shipping_note IS NOT NULL AND f.shipping_note <> '')
                THEN 1 ELSE 0 END                                                     AS ox_shipping,
      CASE WHEN f.composition_info IS NOT NULL OR f.gift_info IS NOT NULL
                OR f.coupon_info IS NOT NULL THEN 1 ELSE 0 END                        AS ox_promo,
      CASE WHEN f.thumbnail_url IS NOT NULL AND f.thumbnail_url <> '' THEN 1 ELSE 0 END AS ox_thumb,
      CASE WHEN f.img_n > 0 THEN 1 ELSE 0 END                                         AS ox_image,
      CASE WHEN f.ai_summary IS NOT NULL AND f.ai_summary <> '' THEN 1 ELSE 0 END      AS ox_summary,
      CASE WHEN f.ai_summary_confidence >= 70 THEN 1 ELSE 0 END                       AS ox_conf70
    FROM (
    SELECT 'ig' AS platform, pp.id AS product_id, p.post_id AS parent_key,
           p.user_id AS owner_id, p.publish_date AS publish_dt, NULL AS external_url,
           p.gonggu_start_date AS sd, p.gonggu_end_date AS ed, p.gonggu_stage AS stage,
           p.classification_note AS cnote,
           pp.product_name, pp.link_location, pp.url_type, pp.candidate_url,
           pp.link_status, pp.sort_order,
           (SELECT COUNT(*) FROM gonggu_post_product s WHERE s.post_id = p.post_id) AS sib_n,
           d.detail_status, d.thumbnail_url, d.brand_name_kr, d.brand_name_en,
           d.category, d.subcategory, d.search_keywords,
           d.original_price, d.sale_price, d.discount_rate, d.discount_amount,
           d.free_shipping, d.shipping_fee, d.shipping_note,
           d.composition_info, d.gift_info, d.coupon_info,
           d.ai_summary, d.ai_summary_confidence, d.detail_error,
           (SELECT COUNT(*) FROM gonggu_post_product_image i
              WHERE i.post_product_id = pp.id) AS img_n,
           (SELECT COUNT(*) FROM gonggu_post_product_image i
              WHERE i.post_product_id = pp.id AND i.image_type = 'thumbnail') AS img_thumb_n
      FROM gonggu_post p
      JOIN gonggu_post_product pp ON pp.post_id = p.post_id
      LEFT JOIN gonggu_post_product_detail d ON d.post_product_id = pp.id
     UNION ALL 
    SELECT 'yt' AS platform, vp.id AS product_id, v.video_id AS parent_key,
           v.channel_id AS owner_id, CAST(v.publishDate AS DATETIME) AS publish_dt,
           v.external_url AS external_url,
           v.gonggu_start_date AS sd, v.gonggu_end_date AS ed, v.gonggu_stage AS stage,
           v.classification_note AS cnote,
           vp.product_name, vp.link_location, vp.url_type, vp.candidate_url,
           vp.link_status, vp.sort_order,
           (SELECT COUNT(*) FROM gonggu_video_product s WHERE s.video_id = v.video_id) AS sib_n,
           d.detail_status, d.thumbnail_url, d.brand_name_kr, d.brand_name_en,
           d.category, d.subcategory, d.search_keywords,
           d.original_price, d.sale_price, d.discount_rate, d.discount_amount,
           d.free_shipping, d.shipping_fee, d.shipping_note,
           d.composition_info, d.gift_info, d.coupon_info,
           d.ai_summary, d.ai_summary_confidence, d.detail_error,
           (SELECT COUNT(*) FROM gonggu_video_product_image i
              WHERE i.video_product_id = vp.id) AS img_n,
           (SELECT COUNT(*) FROM gonggu_video_product_image i
              WHERE i.video_product_id = vp.id AND i.image_type = 'thumbnail') AS img_thumb_n
      FROM gonggu_video v
      JOIN gonggu_video_product vp ON vp.video_id = v.video_id
      LEFT JOIN gonggu_video_product_detail d ON d.video_product_id = vp.id
    ) f;

-- ============ 예제 1) 축별 단독 분포 ============
-- 플랫폼
SELECT platform AS `값`, COUNT(*) AS `건수` FROM v_gonggu_case_axes GROUP BY platform ORDER BY `건수` DESC;

-- 공구기간_NULL패턴
SELECT c_period AS `값`, COUNT(*) AS `건수` FROM v_gonggu_case_axes GROUP BY c_period ORDER BY `건수` DESC;

-- gonggu_stage
SELECT c_stage AS `값`, COUNT(*) AS `건수` FROM v_gonggu_case_axes GROUP BY c_stage ORDER BY `건수` DESC;

-- link_location
SELECT c_loc AS `값`, COUNT(*) AS `건수` FROM v_gonggu_case_axes GROUP BY c_loc ORDER BY `건수` DESC;

-- url_type(LLM판정)
SELECT c_urltype AS `값`, COUNT(*) AS `건수` FROM v_gonggu_case_axes GROUP BY c_urltype ORDER BY `건수` DESC;

-- candidate_url_도메인유형
SELECT c_urlkind AS `값`, COUNT(*) AS `건수` FROM v_gonggu_case_axes GROUP BY c_urlkind ORDER BY `건수` DESC;

-- URL_상세페이지여부
SELECT c_depth AS `값`, COUNT(*) AS `건수` FROM v_gonggu_case_axes GROUP BY c_depth ORDER BY `건수` DESC;

-- link_status
SELECT c_linkstatus AS `값`, COUNT(*) AS `건수` FROM v_gonggu_case_axes GROUP BY c_linkstatus ORDER BY `건수` DESC;

-- detail_status
SELECT c_detail AS `값`, COUNT(*) AS `건수` FROM v_gonggu_case_axes GROUP BY c_detail ORDER BY `건수` DESC;

-- 미해결_세부사유
SELECT c_unres AS `값`, COUNT(*) AS `건수` FROM v_gonggu_case_axes GROUP BY c_unres ORDER BY `건수` DESC;

-- 부모_상품수
SELECT c_multi AS `값`, COUNT(*) AS `건수` FROM v_gonggu_case_axes GROUP BY c_multi ORDER BY `건수` DESC;

-- 유튜브_채널링크
SELECT c_ext AS `값`, COUNT(*) AS `건수` FROM v_gonggu_case_axes GROUP BY c_ext ORDER BY `건수` DESC;

-- ============ 예제 2) 2축 교차표 ============
-- link_status × candidate_url 도메인유형
SELECT c_linkstatus, c_urlkind, COUNT(*) AS `건수` FROM v_gonggu_case_axes GROUP BY c_linkstatus, c_urlkind ORDER BY c_linkstatus, `건수` DESC;

-- link_status × 공구기간 NULL패턴
SELECT c_linkstatus, c_period, COUNT(*) AS `건수` FROM v_gonggu_case_axes GROUP BY c_linkstatus, c_period ORDER BY c_linkstatus, `건수` DESC;

-- link_status × link_location
SELECT c_linkstatus, c_loc, COUNT(*) AS `건수` FROM v_gonggu_case_axes GROUP BY c_linkstatus, c_loc ORDER BY c_linkstatus, `건수` DESC;

-- link_status × URL 상세페이지여부
SELECT c_linkstatus, c_depth, COUNT(*) AS `건수` FROM v_gonggu_case_axes GROUP BY c_linkstatus, c_depth ORDER BY c_linkstatus, `건수` DESC;

-- gonggu_stage × 공구기간 NULL패턴  (종료+종료일X = 강제종료 추정)
SELECT c_stage, c_period, COUNT(*) AS `건수` FROM v_gonggu_case_axes GROUP BY c_stage, c_period ORDER BY c_stage, `건수` DESC;

-- gonggu_stage × link_status
SELECT c_stage, c_linkstatus, COUNT(*) AS `건수` FROM v_gonggu_case_axes GROUP BY c_stage, c_linkstatus ORDER BY c_stage, `건수` DESC;

-- url_type(LLM) × 실제 도메인유형  (불일치 = LLM 오분류 후보)
SELECT c_urltype, c_urlkind, COUNT(*) AS `건수` FROM v_gonggu_case_axes GROUP BY c_urltype, c_urlkind ORDER BY c_urltype, `건수` DESC;

-- detail_status × link_status
SELECT c_detail, c_linkstatus, COUNT(*) AS `건수` FROM v_gonggu_case_axes GROUP BY c_detail, c_linkstatus ORDER BY c_detail, `건수` DESC;

-- detail_status × 도메인유형
SELECT c_detail, c_urlkind, COUNT(*) AS `건수` FROM v_gonggu_case_axes GROUP BY c_detail, c_urlkind ORDER BY c_detail, `건수` DESC;

-- 미해결 세부사유 × 플랫폼
SELECT c_unres, platform, COUNT(*) AS `건수` FROM v_gonggu_case_axes GROUP BY c_unres, platform ORDER BY c_unres, `건수` DESC;

-- 미해결 세부사유 × gonggu_stage
SELECT c_unres, c_stage, COUNT(*) AS `건수` FROM v_gonggu_case_axes GROUP BY c_unres, c_stage ORDER BY c_unres, `건수` DESC;

-- link_location × 도메인유형
SELECT c_loc, c_urlkind, COUNT(*) AS `건수` FROM v_gonggu_case_axes GROUP BY c_loc, c_urlkind ORDER BY c_loc, `건수` DESC;

-- ============ 예제 3) O/X 조합 전수 (한 줄 = 한 케이스) ============
SELECT IF(ox_start, 'O', 'X') AS `시작일`,
       IF(ox_end, 'O', 'X') AS `종료일`,
       IF(ox_note, 'O', 'X') AS `특이사항`,
       IF(ox_url, 'O', 'X') AS `후보URL`,
       IF(ox_linkdone, 'O', 'X') AS `링크확정`,
       IF(ox_naver, 'O', 'X') AS `네이버링크`,
       IF(ox_hub, 'O', 'X') AS `허브링크`,
       IF(ox_detailrow, 'O', 'X') AS `상세행`,
       IF(ox_detaildone, 'O', 'X') AS `상세완료`,
       IF(ox_price, 'O', 'X') AS `판매가`,
       IF(ox_origprice, 'O', 'X') AS `정가`,
       IF(ox_discount, 'O', 'X') AS `할인`,
       IF(ox_brand, 'O', 'X') AS `브랜드`,
       IF(ox_category, 'O', 'X') AS `카테고리`,
       IF(ox_keywords, 'O', 'X') AS `키워드`,
       IF(ox_shipping, 'O', 'X') AS `배송`,
       IF(ox_promo, 'O', 'X') AS `구성·사은·쿠폰`,
       IF(ox_thumb, 'O', 'X') AS `썸네일`,
       IF(ox_image, 'O', 'X') AS `이미지`,
       IF(ox_summary, 'O', 'X') AS `AI요약`,
       IF(ox_conf70, 'O', 'X') AS `신뢰도70+`,
       COUNT(*) AS `건수`
  FROM v_gonggu_case_axes
 GROUP BY ox_start, ox_end, ox_note, ox_url, ox_linkdone, ox_naver, ox_hub, ox_detailrow, ox_detaildone, ox_price, ox_origprice, ox_discount, ox_brand, ox_category, ox_keywords, ox_shipping, ox_promo, ox_thumb, ox_image, ox_summary, ox_conf70
 ORDER BY `건수` DESC;

-- ============ 예제 4) 최대 세분화 라벨 조합 전수 ============
SELECT platform, c_period, c_stage, c_loc, c_urltype, c_urlkind, c_depth, c_linkstatus, c_detail, c_unres, c_multi, c_ext, COUNT(*) AS `건수`
  FROM v_gonggu_case_axes
 GROUP BY platform, c_period, c_stage, c_loc, c_urltype, c_urlkind, c_depth, c_linkstatus, c_detail, c_unres, c_multi, c_ext
 ORDER BY `건수` DESC;

-- ============ 예제 5) 미해결(done 아님)만 세부 사유별 ============
SELECT c_unres, c_linkstatus, c_period, COUNT(*) AS `건수`
  FROM v_gonggu_case_axes
 WHERE link_status IS NULL OR link_status <> 'done'
 GROUP BY c_unres, c_linkstatus, c_period
 ORDER BY `건수` DESC;
