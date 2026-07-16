#!/usr/bin/env python3
"""4단계: load_ready.json(또는 resolve_links.py를 거친 load_ready_resolved.json)을
dev_gongguking의 gonggu_video/gonggu_video_product(유튜브) 또는
gonggu_post/gonggu_post_product(인스타그램)에 INSERT한다.
이미 있는 (post_id) / (video_id)는 건너뛴다(덮어쓰지 않음 — 다운스트림에서 이미 손댔을 수 있음).

사용법:
    python3 scripts/load.py
"""
from common import LOAD_READY_FILE, RESOLVED_FILE, connect_dst, load_json

# resolve_links.py를 돌렸으면 candidate_url이 "찐 최종 링크 하나"로 좁혀진 이 파일을 쓰고,
# 아직 안 돌렸으면(또는 스킵했으면) transform.py 원본(LLM 후보를 세미콜론으로 이어붙인 상태)을 쓴다.
INPUT_FILE = RESOLVED_FILE if RESOLVED_FILE.exists() else LOAD_READY_FILE

INSERT_VIDEO = """
INSERT INTO gonggu_video
    (video_id, channel_id, title, video_url, publishDate, gonggu_start_date, gonggu_end_date, classification_note)
VALUES (%(video_id)s, %(channel_id)s, %(title)s, %(video_url)s, %(publishDate)s,
        %(gonggu_start_date)s, %(gonggu_end_date)s, %(classification_note)s)
"""
CHECK_VIDEO_EXISTS = "SELECT id FROM gonggu_video WHERE video_id = %s"
INSERT_VIDEO_PRODUCT = """
INSERT INTO gonggu_video_product (video_id, product_name, link_location, url_type, candidate_url, sort_order)
VALUES (%(video_id)s, %(product_name)s, %(link_location)s, %(url_type)s, %(candidate_url)s, %(sort_order)s)
"""

INSERT_POST = """
INSERT INTO gonggu_post
    (post_id, user_id, url, publish_date, gonggu_start_date, gonggu_end_date, classification_note)
VALUES (%(post_id)s, %(user_id)s, %(url)s, %(publish_date)s,
        %(gonggu_start_date)s, %(gonggu_end_date)s, %(classification_note)s)
"""
CHECK_POST_EXISTS = "SELECT id FROM gonggu_post WHERE post_id = %s"
INSERT_POST_PRODUCT = """
INSERT INTO gonggu_post_product (post_id, product_name, link_location, url_type, candidate_url, sort_order)
VALUES (%(post_id)s, %(product_name)s, %(link_location)s, %(url_type)s, %(candidate_url)s, %(sort_order)s)
"""


def load_video(cur, parent, products):
    cur.execute(CHECK_VIDEO_EXISTS, (parent['video_id'],))
    if cur.fetchone():
        return False
    cur.execute(INSERT_VIDEO, parent)
    video_id = parent['video_id']  # FK 컬럼명이 gonggu_video_product.video_id로 되어있음(자연키)
    for p in products:
        cur.execute(INSERT_VIDEO_PRODUCT, {**p, 'video_id': video_id})
    return True


def load_post(cur, parent, products):
    cur.execute(CHECK_POST_EXISTS, (parent['post_id'],))
    if cur.fetchone():
        return False
    cur.execute(INSERT_POST, parent)
    post_id = parent['post_id']  # FK 컬럼명이 gonggu_post_product.post_id로 되어있음(자연키)
    for p in products:
        cur.execute(INSERT_POST_PRODUCT, {**p, 'post_id': post_id})
    return True


def main():
    items = load_json(INPUT_FILE)
    print(f'입력 파일: {INPUT_FILE}')
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
