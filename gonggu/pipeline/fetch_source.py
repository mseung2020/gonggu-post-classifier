#!/usr/bin/env python3
"""1단계: hifen DB(읽기 전용)에서 최근 N일치 "공구/공동구매" 키워드 매칭 인스타그램/유튜브
포스트를 뽑아 LLM#1 입력 스키마(description/publish_date/creator_description)로 정규화한다.
원본 컬럼명(post_id/user_id/url/publish_date, video_id/channel_id/publishDate/video_url)은
그대로 들고 있다가 load.py에서 gonggu_post/gonggu_video 컬럼에 그대로 꽂아 넣는다.

사용법:
    DAYS_BACK=7 python3 scripts/fetch_source.py
결과: data/01_raw/<발행일>.jsonl (날짜별 — 이번에 가져온 기간에 해당하는 날짜만 새로 씀)
"""
import datetime
import os

from gonggu.common import RAW_DIR, connect_src, dump_jsonl_sharded, post_date_key

DAYS_BACK = int(os.environ.get('DAYS_BACK', '7'))

# instagram_user 조인은 계정 핸들(username)을 가져오려고 추가(2026-08-21). COLLATE 힌트를
# 붙이지 않는 이유: instagram_post.user_id와 instagram_user.user_id는 둘 다
# utf8mb4_unicode_ci라 그냥 조인된다. 바로 아래 instagram_user_external_url 조인에만 COLLATE가
# 붙어 있는 건 **그 테이블만** utf8mb4_0900_ai_ci를 쓰기 때문이며, 이번 조인과는 무관하다.
# ⚠ GROUP_CONCAT 때문에 GROUP BY가 있으므로 SELECT에 컬럼을 추가하면 GROUP BY에도 반드시
#   같이 추가해야 한다(ONLY_FULL_GROUP_BY에서 에러).
IG_QUERY = """
SELECT p.post_id AS post_id, p.user_id AS user_id, iu.username AS username, p.url AS url,
       p.publish_date AS publish_date, d.description AS caption,
       GROUP_CONCAT(DISTINCT u.external_url SEPARATOR ';') AS creator_bio_urls
FROM instagram_post p
JOIN instagram_post_description d ON d.post_id = p.post_id
LEFT JOIN instagram_user iu ON iu.user_id = p.user_id
LEFT JOIN instagram_user_external_url u ON u.user_id = p.user_id COLLATE utf8mb4_unicode_ci
WHERE p.publish_date >= %s
  AND (d.description LIKE '%%공구%%' OR d.description LIKE '%%공동구매%%')
GROUP BY p.post_id, p.user_id, iu.username, p.url, p.publish_date, d.description
"""

# youtuber_info 조인은 채널명을 가져오려고 추가(2026-08-21). 채널명 출처로 이 테이블을 고른 근거:
#  - YT_video_lists에는 채널명 컬럼 자체가 없다.
#  - brand.channel_title은 PPL 영상만 있어서 그걸 쓰면 이 쿼리(키워드 경로)에서는 못 채운다.
#  - youtuber_info는 PK가 channel_id인 505,735행 테이블이고, 현재 gonggu_video의 고유
#    channel_id 1,363개가 전부 매칭된다(1363/1363, 2026-08-21 실측) — 그래서 이 쿼리와
#    fetch_yt_ppl.py 양쪽에 같은 조인을 걸면 두 경로가 균일하게 채워진다.
# collation도 YT_video_lists.channel_id / youtuber_info.channel_id 모두 utf8mb4_unicode_ci다.
YT_QUERY = """
SELECT d.video_id AS video_id, v.channel_id AS channel_id, yi.title AS channel_name,
       CONCAT('https://www.youtube.com/watch?v=', d.video_id) AS video_url,
       d.publishDate AS publishDate, d.title AS title, d.video_description AS caption
FROM YT_video_lists_detail d
LEFT JOIN YT_video_lists v ON v.video_id = d.video_id
LEFT JOIN youtuber_info yi ON yi.channel_id = v.channel_id
WHERE d.publishDate >= %s
  AND (d.video_description LIKE '%%공구%%' OR d.video_description LIKE '%%공동구매%%')
"""


def fetch_ig(conn, since):
    with conn.cursor() as cur:
        cur.execute(IG_QUERY, (since,))
        rows = cur.fetchall()
    out = []
    for r in rows:
        out.append({
            'platform': 'ig',
            'post_id': r['post_id'],
            'user_id': r['user_id'],
            # username/channel_name은 "수집 시점의 이름" 스냅샷이다 — 계정이 핸들을 바꾸면
            # 저장값은 과거값이 된다(queries/add_creator_names.sql 참고). 없으면 None을 유지해
            # 백필이 다시 시도할 수 있게 한다(빈 문자열로 만들면 IS NULL 대상에서 빠진다).
            'username': r['username'] or None,
            'url': r['url'],
            'publish_date': str(r['publish_date']),
            'description': r['caption'] or '',
            # caption_raw: LLM#1 입력용으로 가공되지 않은 원문 그대로. 확정 공구만
            # gonggu_post.description에 적재된다(2026-08-21). 인스타는 description과 값이
            # 같지만, 유튜브는 description에 제목이 붙으므로 두 플랫폼 모두 이 키를 "원문의
            # 유일한 출처"로 두고 downstream(transform)을 플랫폼 분기 없이 통일한다.
            'caption_raw': r['caption'] or '',
            'creator_description': r['creator_bio_urls'] or '',
        })
    return out


def fetch_yt(conn, since):
    with conn.cursor() as cur:
        cur.execute(YT_QUERY, (since,))
        rows = cur.fetchall()
    out = []
    for r in rows:
        out.append({
            'platform': 'yt',
            'video_id': r['video_id'],
            'channel_id': r['channel_id'],
            'channel_name': r['channel_name'] or None,   # 스냅샷 — 위 fetch_ig 주석 참고
            'video_url': r['video_url'],
            'publishDate': str(r['publishDate']),
            'title': r['title'] or '',
            # description은 LLM#1 입력용 가공값(제목을 앞에 붙임) — 그대로 DB에 넣으면 제목이
            # 중복되므로, DB 적재용 원문은 caption_raw를 쓴다(위 fetch_ig 주석 참고).
            'description': f"[제목] {r['title'] or ''}\n\n{r['caption'] or ''}",
            'caption_raw': r['caption'] or '',
            'creator_description': '',
        })
    return out


def main():
    since = datetime.date.today() - datetime.timedelta(days=DAYS_BACK)
    conn = connect_src()
    try:
        ig = fetch_ig(conn, since)
        yt = fetch_yt(conn, since)
    finally:
        conn.close()
    posts = ig + yt
    dump_jsonl_sharded(RAW_DIR, posts, post_date_key)
    print(f'{since} 이후 — ig {len(ig)}건, yt {len(yt)}건, 총 {len(posts)}건 -> {RAW_DIR}/*.jsonl (날짜별)')


if __name__ == '__main__':
    main()
