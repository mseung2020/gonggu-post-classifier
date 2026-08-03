#!/usr/bin/env python3
"""일회성 백필 — candidate_url에 세미콜론(;)으로 여러 후보가 남아있는 기존 DB 행을 전부
훑어서, 새 ranking.py + resolve_product()로 대표 URL 1개로 정리한다.

2026-07-29 결정("DB의 candidate_url엔 항상 링크 1개만") 이후로 신규 처리 건은
resolve_links/rescan_inprogress.py가 알아서 1개로 남기지만, 그 전에 이미 적재된 행들은
그대로 여러 개가 남아있어서 이 스크립트로 한 번만 정리한다. 일일 파이프라인에는 포함하지
않음 — 다 정리되면 다시 쓸 일 없는 임시 스크립트.

gonggu_stage/link_status 조건 없이 "candidate_url에 ';'이 있는 행 전부"를 대상으로 한다
(rescan_inprogress.py와 달리 hold/unresolved 여부나 진행 단계를 가리지 않음 — 지금 있는
걸 한 번에 청소하는 게 목적이라).

사용법:
    python3 scripts/_backfill_collapse_candidates.py            # 전체 대상
    LIMIT=50 python3 scripts/_backfill_collapse_candidates.py   # 앞에서 50건만(테스트용)
    BACKFILL_CONCURRENCY=6 python3 scripts/_backfill_collapse_candidates.py
"""
import os
import queue
import sys
import threading
import time

from playwright.sync_api import sync_playwright

from common import DEEPSEEK_KEY, append_jsonl, connect_dst
from resolve_links.browser import new_context_page
from resolve_links.config import AUTH_STATE_FILE, ITEM_DELAY, RESOLUTION_FILE
from resolve_links.core import resolve_product
from resolve_links.matching import product_key

BACKFILL_CONCURRENCY = int(os.environ.get('BACKFILL_CONCURRENCY', '4'))

SELECT_POST = """
SELECT pp.id AS row_id, pp.product_name, pp.link_location, pp.url_type, pp.candidate_url,
       pp.sort_order, p.post_id, p.user_id, p.url, p.publish_date, p.classification_note
FROM gonggu_post_product pp
JOIN gonggu_post p ON p.post_id = pp.post_id
WHERE pp.candidate_url LIKE '%;%'
"""

SELECT_VIDEO = """
SELECT vp.id AS row_id, vp.product_name, vp.link_location, vp.url_type, vp.candidate_url,
       vp.sort_order, v.video_id, v.channel_id, v.video_url, v.publishDate, v.classification_note
FROM gonggu_video_product vp
JOIN gonggu_video v ON v.video_id = vp.video_id
WHERE vp.candidate_url LIKE '%;%'
"""

UPDATE_POST = 'UPDATE gonggu_post_product SET candidate_url = %s, link_status = %s WHERE id = %s'
UPDATE_VIDEO = 'UPDATE gonggu_video_product SET candidate_url = %s, link_status = %s WHERE id = %s'


def _fetch_targets(conn):
    targets = []
    with conn.cursor() as cur:
        cur.execute(SELECT_POST)
        for r in cur.fetchall():
            parent = {'post_id': r['post_id'], 'user_id': r['user_id'], 'url': r['url'],
                      'publish_date': str(r['publish_date']), 'classification_note': r['classification_note']}
            product = {'product_name': r['product_name'], 'link_location': r['link_location'],
                       'url_type': r['url_type'], 'candidate_url': r['candidate_url'],
                       'sort_order': r['sort_order']}
            targets.append(('ig', parent, product, r['row_id']))

        cur.execute(SELECT_VIDEO)
        for r in cur.fetchall():
            parent = {'video_id': r['video_id'], 'channel_id': r['channel_id'], 'video_url': r['video_url'],
                      'publishDate': str(r['publishDate']), 'classification_note': r['classification_note']}
            product = {'product_name': r['product_name'], 'link_location': r['link_location'],
                       'url_type': r['url_type'], 'candidate_url': r['candidate_url'],
                       'sort_order': r['sort_order']}
            targets.append(('yt', parent, product, r['row_id']))
    return targets


def _worker(worker_id, work_q, lock, counters, total, save_auth_state):
    db = connect_dst()
    try:
        with sync_playwright() as pw:
            browser, ctx, page = new_context_page(pw)
            while True:
                try:
                    platform, parent, product, row_id = work_q.get_nowait()
                except queue.Empty:
                    break
                try:
                    res = resolve_product(page, platform, parent, product)
                except Exception as e:
                    res = {'status': 'error', 'final_url': None,
                           'candidate_url': product['candidate_url'].split(';')[0], 'note': str(e)[:160]}

                key = product_key(platform, parent, product['sort_order'])
                update_sql = UPDATE_POST if platform == 'ig' else UPDATE_VIDEO
                new_candidate_url = (res.get('candidate_url') or product['candidate_url'].split(';')[0])[:500]

                with lock:
                    with db.cursor() as cur:
                        cur.execute(update_sql, (new_candidate_url, res['status'], row_id))
                    db.commit()
                    append_jsonl(RESOLUTION_FILE, {**res, 'key': key})
                    counters[res['status']] = counters.get(res['status'], 0) + 1
                    counters['_done_n'] = counters.get('_done_n', 0) + 1
                    shown = new_candidate_url
                    print(f"  [{counters['_done_n']}/{total}] (w{worker_id}) {key} -> {res['status']} {shown[:70]}",
                          flush=True)
                time.sleep(ITEM_DELAY)
            if save_auth_state:
                AUTH_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
                ctx.storage_state(path=str(AUTH_STATE_FILE))
            browser.close()
    finally:
        db.close()


def main():
    if not DEEPSEEK_KEY:
        print('.env에 DEEPSEEK_KEY가 필요합니다.', file=sys.stderr)
        sys.exit(1)

    conn = connect_dst()
    try:
        targets = _fetch_targets(conn)
    finally:
        conn.close()

    limit = int(os.environ.get('LIMIT', '0')) or len(targets)
    targets = targets[:limit]
    print(f'다중 후보(candidate_url에 ";") 정리 대상 {len(targets)}건 (동시 워커 {BACKFILL_CONCURRENCY}개)')
    if not targets:
        return

    work_q = queue.Queue()
    for t in targets:
        work_q.put(t)
    lock = threading.Lock()
    counters = {}
    n_workers = max(1, min(BACKFILL_CONCURRENCY, len(targets)))
    threads = [
        threading.Thread(target=_worker, args=(wid, work_q, lock, counters, len(targets), wid == 0))
        for wid in range(n_workers)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    by_status = {k: v for k, v in counters.items() if not k.startswith('_')}
    print(f'백필 완료 {len(targets)}건 — {by_status}')


if __name__ == '__main__':
    main()
