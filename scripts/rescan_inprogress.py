#!/usr/bin/env python3
"""5단계 보강: gonggu_stage='진행중'인데 link_status='unresolved'인 상품만 골라 링크 해석을
다시 시도한다. 시작전 단계에서는 인포크 등에 아직 구매 링크가 없어서 못 찾았을 뿐인데,
진행중이 되면 실제로 링크가 채워지는 경우가 많아서(원준님 피드백 반영) 이 전이 시점을
노려 재탐색한다.

resolve_links의 실제 판단/크롤링 로직(resolve_product)과 안티봇 대응(도메인당 동시 접근
제한 — browser.fetch()/redirect.follow_redirect() 내부의 domain_gate)을 그대로 재사용한다.
load_ready/link_resolution 파일을 거치지 않고 DB에서 직접 대상을 뽑아 DB에 직접 반영한다 —
unresolved 상품의 candidate_url엔 LLM이 뽑은 원본 후보가 그대로 보존되어 있어(build_resolved_file이
done일 때만 덮어씀) DB만으로 재시도에 필요한 정보가 충분하다.

link_resolution.jsonl에도 같은 키로 결과를 append해서, 정기 파이프라인이 나중에
04_resolved를 다시 조립할 때 이 재탐색 결과가 잊히지 않게 한다(파일과 DB가 항상 같은
진실을 가리키게 유지).

여전히 unresolved면 그대로 둔다 — 이번에도 못 찾았으면 다음 번 진행중 재탐색이나 다른
보강 전까지는 어쩔 수 없음.

사용법:
    python3 scripts/rescan_inprogress.py            # 전체 대상
    LIMIT=50 python3 scripts/rescan_inprogress.py   # 앞에서 50건만(테스트용)
    RESCAN_CONCURRENCY=4 python3 scripts/rescan_inprogress.py
"""
import os
import queue
import sys
import threading
import time

from playwright.sync_api import sync_playwright

from common import append_jsonl, connect_dst
from resolve_links.browser import new_context_page
from resolve_links.config import AUTH_STATE_FILE, DIFY_KEY_JUDGE, DIFY_KEY_PICK, ITEM_DELAY, RESOLUTION_FILE
from resolve_links.core import resolve_product
from resolve_links.matching import product_key

RESCAN_CONCURRENCY = int(os.environ.get('RESCAN_CONCURRENCY', '4'))

SELECT_POST = """
SELECT pp.id AS row_id, pp.product_name, pp.link_location, pp.url_type, pp.candidate_url,
       pp.sort_order, p.post_id, p.user_id, p.url, p.publish_date, p.classification_note
FROM gonggu_post_product pp
JOIN gonggu_post p ON p.post_id = pp.post_id
WHERE p.gonggu_stage = '진행중' AND pp.link_status = 'unresolved'
"""

SELECT_VIDEO = """
SELECT vp.id AS row_id, vp.product_name, vp.link_location, vp.url_type, vp.candidate_url,
       vp.sort_order, v.video_id, v.channel_id, v.video_url, v.publishDate, v.classification_note
FROM gonggu_video_product vp
JOIN gonggu_video v ON v.video_id = vp.video_id
WHERE v.gonggu_stage = '진행중' AND vp.link_status = 'unresolved'
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
    # DB 커넥션은 워커(스레드)당 1개만 열어서 재사용한다 — pymysql 커넥션은 스레드 간 공유가
    # 안전하지 않으므로 워커마다 독립된 커넥션을 갖고, 상품마다 새로 열고 닫는 오버헤드도 없앤다.
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
                    res = {'status': 'error', 'final_url': None, 'note': str(e)[:160]}

                key = product_key(platform, parent, product['sort_order'])
                update_sql = UPDATE_POST if platform == 'ig' else UPDATE_VIDEO
                new_candidate_url = res['final_url'][:500] if res.get('status') == 'done' and res.get('final_url') \
                    else product['candidate_url']

                with lock:
                    with db.cursor() as cur:
                        cur.execute(update_sql, (new_candidate_url, res['status'], row_id))
                    db.commit()
                    append_jsonl(RESOLUTION_FILE, {**res, 'key': key})
                    counters[res['status']] = counters.get(res['status'], 0) + 1
                    counters['_done_n'] = counters.get('_done_n', 0) + 1
                    shown = res.get('final_url') or res.get('note', '')
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
    if not DIFY_KEY_PICK or not DIFY_KEY_JUDGE:
        print('.env에 DIFY_KEY_PICK / DIFY_KEY_JUDGE가 필요합니다.', file=sys.stderr)
        sys.exit(1)

    conn = connect_dst()
    try:
        targets = _fetch_targets(conn)
    finally:
        conn.close()

    limit = int(os.environ.get('LIMIT', '0')) or len(targets)
    targets = targets[:limit]
    print(f'진행중 + unresolved 재탐색 대상 {len(targets)}건 (동시 워커 {RESCAN_CONCURRENCY}개)')
    if not targets:
        return

    work_q = queue.Queue()
    for t in targets:
        work_q.put(t)
    lock = threading.Lock()
    counters = {}
    n_workers = max(1, min(RESCAN_CONCURRENCY, len(targets)))
    threads = [
        threading.Thread(target=_worker, args=(wid, work_q, lock, counters, len(targets), wid == 0))
        for wid in range(n_workers)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    by_status = {k: v for k, v in counters.items() if not k.startswith('_')}
    print(f'재탐색 완료 {len(targets)}건 — {by_status}')


if __name__ == '__main__':
    main()
