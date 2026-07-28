#!/usr/bin/env python3
"""제품 카테고리 분류(gonggu_category_classify.yml)용 입력 데이터를 만든다.
dev_gongguking(DST)의 gonggu_post_product/gonggu_video_product에서 제품명을,
hifen(SRC)의 instagram_post_description/YT_video_lists_detail에서 설명(캡션)을 가져와
post_id/video_id 자연키로 파이썬에서 합친다 — 두 DB가 서로 다른 서버라 SQL JOIN이 안 됨.

사용법:
    python3 scripts/build_category_dataset.py [출력경로]
    (출력경로 생략 시 ~/Desktop/gonggu_category_input.jsonl)
"""
import json
import pathlib
import sys

from common import connect_dst, connect_src

OUT_DEFAULT = pathlib.Path.home() / 'Desktop' / 'gonggu_category_input.jsonl'

FETCH_POST_PRODUCTS = """
SELECT pp.post_id AS content_id, pp.product_name AS product_name, p.gonggu_stage AS gonggu_stage
FROM gonggu_post_product pp
JOIN gonggu_post p ON p.post_id = pp.post_id
"""

FETCH_VIDEO_PRODUCTS = """
SELECT vp.video_id AS content_id, vp.product_name AS product_name, v.title AS title,
       v.gonggu_stage AS gonggu_stage
FROM gonggu_video_product vp
JOIN gonggu_video v ON v.video_id = vp.video_id
"""


def _chunks(seq, n=1000):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def fetch_descriptions(conn, ids, table, id_col, desc_col):
    out = {}
    ids = sorted(set(ids))
    with conn.cursor() as cur:
        for chunk in _chunks(ids):
            fmt = ','.join(['%s'] * len(chunk))
            cur.execute(f'SELECT {id_col}, {desc_col} FROM {table} WHERE {id_col} IN ({fmt})', chunk)
            for r in cur.fetchall():
                out[r[id_col]] = r[desc_col] or ''
    return out


def main():
    out_path = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else OUT_DEFAULT

    dst = connect_dst()
    try:
        with dst.cursor() as cur:
            cur.execute(FETCH_POST_PRODUCTS)
            post_rows = cur.fetchall()
            cur.execute(FETCH_VIDEO_PRODUCTS)
            video_rows = cur.fetchall()
    finally:
        dst.close()

    src = connect_src()
    try:
        desc_ig = fetch_descriptions(
            src, [r['content_id'] for r in post_rows],
            'instagram_post_description', 'post_id', 'description')
        desc_yt = fetch_descriptions(
            src, [r['content_id'] for r in video_rows],
            'YT_video_lists_detail', 'video_id', 'video_description')
    finally:
        src.close()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, 'w', encoding='utf-8') as f:
        for r in post_rows:
            rec = {
                'platform': '인스타',
                'content_id': r['content_id'],
                'product_name': r['product_name'],
                'title': '',
                'description': desc_ig.get(r['content_id'], ''),
                'gonggu_stage': r['gonggu_stage'],
            }
            f.write(json.dumps(rec, ensure_ascii=False) + '\n')
        for r in video_rows:
            rec = {
                'platform': '유튜브',
                'content_id': r['content_id'],
                'product_name': r['product_name'],
                'title': r['title'] or '',
                'description': desc_yt.get(r['content_id'], ''),
                'gonggu_stage': r['gonggu_stage'],
            }
            f.write(json.dumps(rec, ensure_ascii=False) + '\n')

    print(f'{len(post_rows) + len(video_rows)}건 저장 -> {out_path}')


if __name__ == '__main__':
    main()
