#!/usr/bin/env python3
"""4단계: load_ready.json을 dev_gongguking의 gonggu_video/gonggu_video_product
(유튜브) 또는 gonggu_post/gonggu_post_product(인스타그램)에 INSERT한다.
이미 있는 (post_id) / (video_id)는 건너뛴다(덮어쓰지 않음 — 다운스트림에서 이미 손댔을 수 있음).

사용법:
    python3 scripts/load.py
"""
from common import LOAD_READY_FILE, connect_dst, load_json

INSERT_VIDEO = """
INSERT INTO gonggu_video
    (video_id, channel_id, title, video_url, publishDate, gonggu_start_date, gonggu_end_date, classification_note)
VALUES (%(video_id)s, %(channel_id)s, %(title)s, %(video_url)s, %(publishDate)s,
        %(gonggu_start_date)s, %(gonggu_end_date)s, %(classification_note)s)
"""
CHECK_VIDEO_EXISTS = "SELECT id FROM gonggu_video WHERE video_id = %s"
INSERT_VIDEO_PRODUCT = """
INSERT INTO gonggu_video_product (gonggu_id, product_name, link_location, url_type, candidate_url, sort_order)
VALUES (%(gonggu_id)s, %(product_name)s, %(link_location)s, %(url_type)s, %(candidate_url)s, %(sort_order)s)
"""

INSERT_POST = """
INSERT INTO gonggu_post
    (post_id, user_id, url, publish_date, gonggu_start_date, gonggu_end_date, classification_note)
VALUES (%(post_id)s, %(user_id)s, %(url)s, %(publish_date)s,
        %(gonggu_start_date)s, %(gonggu_end_date)s, %(classification_note)s)
"""
CHECK_POST_EXISTS = "SELECT id FROM gonggu_post WHERE post_id = %s"
INSERT_POST_PRODUCT = """
INSERT INTO gonggu_post_product (gonggu_id, product_name, link_location, url_type, candidate_url, sort_order)
VALUES (%(gonggu_id)s, %(product_name)s, %(link_location)s, %(url_type)s, %(candidate_url)s, %(sort_order)s)
"""


def load_video(cur, parent, products):
    cur.execute(CHECK_VIDEO_EXISTS, (parent['video_id'],))
    if cur.fetchone():
        return False
    cur.execute(INSERT_VIDEO, parent)
    gonggu_id = parent['video_id']  # 자연키(video_id)를 그대로 FK 값으로 씀 — 서로게이트 id 대신
    for p in products:
        cur.execute(INSERT_VIDEO_PRODUCT, {**p, 'gonggu_id': gonggu_id})
    return True


def load_post(cur, parent, products):
    cur.execute(CHECK_POST_EXISTS, (parent['post_id'],))
    if cur.fetchone():
        return False
    cur.execute(INSERT_POST, parent)
    gonggu_id = parent['post_id']  # 자연키(post_id)를 그대로 FK 값으로 씀 — 서로게이트 id 대신
    for p in products:
        cur.execute(INSERT_POST_PRODUCT, {**p, 'gonggu_id': gonggu_id})
    return True


def main():
    items = load_json(LOAD_READY_FILE)
    conn = connect_dst()
    inserted, skipped = 0, 0
    try:
        with conn.cursor() as cur:
            for item in items:
                fn = load_video if item['platform'] == 'yt' else load_post
                ok = fn(cur, item['parent'], item['products'])
                if ok:
                    inserted += 1
                else:
                    skipped += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    print(f'삽입 {inserted}건 / 이미 존재해서 스킵 {skipped}건 (전체 {len(items)}건)')


if __name__ == '__main__':
    main()
