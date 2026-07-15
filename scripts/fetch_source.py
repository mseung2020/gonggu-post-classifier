#!/usr/bin/env python3
"""1단계: hifen DB(읽기 전용)에서 최근 N일치 "공구/공동구매" 키워드 매칭 인스타그램/유튜브
포스트를 뽑아 LLM#1 입력 스키마(description/publish_date/creator_description)로 정규화한다.
원본 컬럼명(post_id/user_id/url/publish_date, video_id/channel_id/publishDate/video_url)은
그대로 들고 있다가 load.py에서 gonggu_post/gonggu_video 컬럼에 그대로 꽂아 넣는다.

사용법:
    DAYS_BACK=7 python3 scripts/fetch_source.py
결과: data/raw/posts_raw.json
"""
import datetime
import os

from common import RAW_FILE, connect_src, dump_json

DAYS_BACK = int(os.environ.get('DAYS_BACK', '7'))

IG_QUERY = """
SELECT p.post_id AS post_id, p.user_id AS user_id, p.url AS url,
       p.publish_date AS publish_date, d.description AS caption,
       GROUP_CONCAT(DISTINCT u.external_url SEPARATOR ';') AS creator_bio_urls
FROM instagram_post p
JOIN instagram_post_description d ON d.post_id = p.post_id
LEFT JOIN instagram_user_external_url u ON u.user_id = p.user_id
WHERE p.publish_date >= %s
  AND (d.description LIKE '%%공구%%' OR d.description LIKE '%%공동구매%%')
GROUP BY p.post_id, p.user_id, p.url, p.publish_date, d.description
"""

YT_QUERY = """
SELECT d.video_id AS video_id, v.channel_id AS channel_id,
       CONCAT('https://www.youtube.com/watch?v=', d.video_id) AS video_url,
       d.publishDate AS publishDate, d.title AS title, d.video_description AS caption
FROM YT_video_lists_detail d
LEFT JOIN YT_video_lists v ON v.video_id = d.video_id
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
            'url': r['url'],
            'publish_date': str(r['publish_date']),
            'description': r['caption'] or '',
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
            'video_url': r['video_url'],
            'publishDate': str(r['publishDate']),
            'title': r['title'] or '',
            'description': f"[제목] {r['title'] or ''}\n\n{r['caption'] or ''}",
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
    dump_json(RAW_FILE, posts)
    print(f'{since} 이후 — ig {len(ig)}건, yt {len(yt)}건, 총 {len(posts)}건 -> {RAW_FILE}')


if __name__ == '__main__':
    main()
