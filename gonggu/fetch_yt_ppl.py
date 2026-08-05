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

YT_PPL_QUERY = """
SELECT video_id, channel_id, title, video_url, video_description AS caption,
       brand1, sponsored_type, publishDate
FROM brand
WHERE publishDate >= %s
  AND COALESCE(video_description, '') NOT LIKE '%%공구%%'
  AND COALESCE(video_description, '') NOT LIKE '%%공동구매%%'
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
            'video_url': r['video_url'],
            'publishDate': str(r['publishDate']),
            'title': r['title'] or '',
            'description': f"[제목] {r['title'] or ''}\n\n{r['caption'] or ''}",
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
