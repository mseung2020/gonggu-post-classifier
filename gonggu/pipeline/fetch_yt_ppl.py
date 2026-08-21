#!/usr/bin/env python3
"""신규 모듈(기존 파이프라인과 완전 독립) 1/2 — hifen DB의 brand 테이블(유튜브 PPL/브랜드
협찬 영상 전체)에서 "공구"/"공동구매" 키워드가 없는 것만 가져와 data/01_raw_yt_ppl/에
저장한다. LLM 호출은 없는 순수 fetch 단계 — 판별은 classify_yt_ppl.py가 담당한다.

"공구"/"공동구매" 키워드가 있는 영상은 fetch_source.py가 이미 처리하므로 이 쿼리 자체에서
제외한다 — 두 fetch 경로가 다루는 video_id가 SQL 단계에서부터 상호 배타적이라 겹치지 않는다.

사용법:
    DAYS_BACK=7 python3 scripts/fetch_yt_ppl.py
결과: data/01_raw_yt_ppl/<발행일>.jsonl (이번에 가져온 기간에 해당하는 날짜 파일만 새로 씀,
    fetch_source.py의 data/01_raw/와는 별도 디렉터리라 서로 덮어쓰지 않음)
"""
import datetime
import os

from gonggu.common import ROOT, connect_src, dump_jsonl_sharded, post_date_key

DAYS_BACK = int(os.environ.get('DAYS_BACK', '7'))
RAW_DIR_YT_PPL = ROOT / 'data/01_raw_yt_ppl'

# youtuber_info 조인으로 채널명을 가져온다(2026-08-21) — fetch_source.py의 키워드 경로와
# **같은 출처**를 쓰는 게 핵심이다. brand 테이블에도 channel_title이 있지만 그건 PPL 영상에만
# 있어서, 그걸 쓰면 PPL로 들어온 영상만 채널명이 채워지는 절반짜리 컬럼이 된다.
# ⚠ 조인이 생기면서 title이 모호해졌다(brand.title = 영상 제목, youtuber_info.title = 채널명).
#   그래서 brand를 b로 alias하고 모든 컬럼을 명시적으로 한정한다 — 한정 안 하면 MySQL이
#   "Column 'title' in field list is ambiguous"로 즉시 실패한다.
YT_PPL_QUERY = """
SELECT b.video_id, b.channel_id, yi.title AS channel_name, b.title AS title,
       b.video_url, b.video_description AS caption,
       b.brand1, b.sponsored_type, b.publishDate
FROM brand b
LEFT JOIN youtuber_info yi ON yi.channel_id = b.channel_id
WHERE b.publishDate >= %s
  AND COALESCE(b.video_description, '') NOT LIKE '%%공구%%'
  AND COALESCE(b.video_description, '') NOT LIKE '%%공동구매%%'
"""


def fetch_yt_ppl(conn, since):
    with conn.cursor() as cur:
        cur.execute(YT_PPL_QUERY, (since,))
        rows = cur.fetchall()
    out = []
    for r in rows:
        out.append({
            'platform': 'yt',
            'video_id': r['video_id'],
            'channel_id': r['channel_id'],
            'channel_name': r['channel_name'] or None,   # 수집 시점 스냅샷(add_creator_names.sql)
            'video_url': r['video_url'],
            'publishDate': str(r['publishDate']),
            'title': r['title'] or '',
            # description은 LLM 입력용 가공값(제목을 앞에 붙임). DB 적재용 원문은 caption_raw —
            # fetch_source.py와 같은 키를 쓰므로 transform은 두 경로를 구분하지 않아도 된다
            # (2026-08-21, gonggu_video.description 추가).
            'description': f"[제목] {r['title'] or ''}\n\n{r['caption'] or ''}",
            'caption_raw': r['caption'] or '',
            'brand_name': r['brand1'] or '',
            'sponsored_type': r['sponsored_type'],
        })
    return out


def main():
    since = datetime.date.today() - datetime.timedelta(days=DAYS_BACK)
    conn = connect_src()
    try:
        rows = fetch_yt_ppl(conn, since)
    finally:
        conn.close()
    dump_jsonl_sharded(RAW_DIR_YT_PPL, rows, post_date_key)
    print(f'{since} 이후 brand 테이블(공구 키워드 제외) {len(rows)}건 -> {RAW_DIR_YT_PPL}/*.jsonl (날짜별)')


if __name__ == '__main__':
    main()
