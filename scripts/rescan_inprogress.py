#!/usr/bin/env python3
"""5단계 보강: gonggu_stage='진행중'인데 link_status가 'unresolved'/'hold'인 상품만 골라 링크
해석을 다시 시도한다. 시작전 단계에서는 인포크 등에 아직 구매 링크가 없어서 못 찾았을 뿐인데,
진행중이 되면 실제로 링크가 채워지는 경우가 많아서(원준님 피드백 반영) 이 전이 시점을
노려 재탐색한다. hold도 unresolved와 동일하게 취급한다(2026-07-29 결정) — hold도 결국
candidate_url이 허브 URL 하나로 남아있는 경우가 많아서 재확인할 가치가 같다.

resolve_links의 실제 판단/크롤링 로직(resolve_product)과 안티봇 대응(도메인당 동시 접근
제한 — browser.fetch()/redirect.follow_redirect() 내부의 domain_gate)을 그대로 재사용한다.
load_ready/link_resolution 파일을 거치지 않고 DB에서 직접 대상을 뽑아 DB에 직접 반영한다.
candidate_url은 성공/실패와 무관하게 항상 대표 URL 1개다(resolve_product가 반환하는
candidate_url 필드 참고) — 원본 다중 후보를 DB에 보존해뒀다가 다시 꺼내 쓰는 방식이 아니라,
매번 그 시점의 candidate_url(대부분 링크인바이오 허브 URL) 하나를 다시 열어봐서 "그 사이에
새 링크가 붙었는지"만 확인하는 것이 이 재탐색의 목적이다 — 예전에 시도했다가 버린 죽은
후보(naver.me 단축링크 등)까지 다시 살려서 재시도할 필요는 없다.

link_resolution.jsonl에도 같은 키로 결과를 append해서, 정기 파이프라인이 나중에
04_resolved를 다시 조립할 때 이 재탐색 결과가 잊히지 않게 한다(파일과 DB가 항상 같은
진실을 가리키게 유지).

여전히 unresolved/hold면 그대로 둔다 — 이번에도 못 찾았으면 다음 번 진행중 재탐색이나 다른
보강 전까지는 어쩔 수 없음.

사용법:
    python3 scripts/rescan_inprogress.py            # 전체 대상
    LIMIT=50 python3 scripts/rescan_inprogress.py   # 앞에서 50건만(테스트용)
    RESCAN_CONCURRENCY=40 python3 scripts/rescan_inprogress.py

RESCAN_CONCURRENCY는 "동시에 처리 중인 상품 수"지 "동시에 뜨는 크롬 수"가 아니다 — 실제
브라우저 개수는 MAX_BROWSERS가 따로 제한한다(resolve_links/browser.py의 LazyPage). 다만
브라우저가 필요한 건의 처리량은 결국 MAX_BROWSERS가 정하므로, 워커만 무작정 올려도 그만큼
빨라지지는 않는다(둘의 차이가 3배를 넘으면 실행 시작 시 경고가 뜬다).
"""
import os
import queue
import sys
import threading
import time

from playwright.sync_api import sync_playwright

from common import DEEPSEEK_KEY, append_jsonl, connect_dst
from resolve_links.browser import LazyPage
from resolve_links.config import HTTP_FAST_PATH, ITEM_DELAY, MAX_BROWSERS, RESOLUTION_FILE
from resolve_links.core import resolve_product
from resolve_links.httpfetch import stats as httpfetch_stats
from resolve_links.matching import product_key

RESCAN_CONCURRENCY = int(os.environ.get('RESCAN_CONCURRENCY', '4'))

SELECT_POST = """
SELECT pp.id AS row_id, pp.product_name, pp.link_location, pp.url_type, pp.candidate_url,
       pp.sort_order, p.post_id, p.user_id, p.url, p.publish_date, p.classification_note
FROM gonggu_post_product pp
JOIN gonggu_post p ON p.post_id = pp.post_id
WHERE p.gonggu_stage = '진행중' AND pp.link_status IN ('unresolved', 'hold')
"""

SELECT_VIDEO = """
SELECT vp.id AS row_id, vp.product_name, vp.link_location, vp.url_type, vp.candidate_url,
       vp.sort_order, v.video_id, v.channel_id, v.video_url, v.publishDate, v.classification_note
FROM gonggu_video_product vp
JOIN gonggu_video v ON v.video_id = vp.video_id
WHERE v.gonggu_stage = '진행중' AND vp.link_status IN ('unresolved', 'hold')
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
            # 브라우저는 실제로 필요해지는 첫 순간까지 미루고, 동시에 뜨는 개수도 MAX_BROWSERS로
            # 제한한다(resolve_links/browser.py의 LazyPage 참고) — 예전엔 여기서 워커마다 무조건
            # new_context_page()를 불러서 사실상 "RESCAN_CONCURRENCY = 크롬 프로세스 수"였다.
            # 기본값 4로 돌 땐 안 드러났지만 실제로는 RESCAN_CONCURRENCY=100으로도 돌리는데,
            # 그러면 크롬 100개가 한꺼번에 떠서 2026-07-30에 시스템을 먹통으로 만든 그 상황
            # (워커 200개 = 크롬 관련 프로세스 550개+, 스왑 32GB 소진)이 그대로 재현된다.
            # 재탐색 대상은 대부분 링크인바이오 허브 URL 하나를 다시 열어보는 것이라 requests
            # 패스트패스로 끝나는 비율이 높아서, 지연 생성의 이득도 크다.
            page = LazyPage(pw, save_auth_state=save_auth_state)
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
                # candidate_url은 상태와 무관하게 항상 단일 URL — resolve_product가 성공/실패
                # 어느 쪽이든 대표 URL 1개를 candidate_url에 담아 반환한다(2026-07-29 결정).
                new_candidate_url = (res.get('candidate_url') or product['candidate_url'])[:500]

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
                # 브라우저를 더 안 쓰게 됐는데 기다리는 워커가 있으면 넘겨준다(runner.py와 동일).
                page.release_if_contended()
                time.sleep(ITEM_DELAY)
            page.close()
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
    if not targets:
        print('진행중 + unresolved 재탐색 대상 0건')
        return

    work_q = queue.Queue()
    for t in targets:
        work_q.put(t)
    lock = threading.Lock()
    counters = {}
    n_workers = max(1, min(RESCAN_CONCURRENCY, len(targets)))
    print(f'진행중 + unresolved 재탐색 대상 {len(targets)}건 — 동시 워커 {n_workers}개, '
          f'브라우저 상한 {MAX_BROWSERS}개, requests 패스트패스 {"ON" if HTTP_FAST_PATH else "OFF"}')
    if n_workers > MAX_BROWSERS * 3:
        print(f'  ⚠ 워커({n_workers})가 브라우저 상한({MAX_BROWSERS})의 3배를 넘습니다 — 브라우저가 '
              f'필요한 건이 많으면 재기동 오버헤드로 오히려 느려질 수 있습니다. '
              f'MAX_BROWSERS를 올리거나 RESCAN_CONCURRENCY를 낮춰보세요.')
    threads = [
        threading.Thread(target=_worker, args=(wid, work_q, lock, counters, len(targets), wid == 0))
        for wid in range(n_workers)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    hs = httpfetch_stats()
    if hs['tried']:
        print(f"requests 패스트패스: {hs['hit']}/{hs['tried']}건 적중 "
              f"({100 * hs['hit'] / hs['tried']:.1f}%) — 나머지는 브라우저로 폴백")
    by_status = {k: v for k, v in counters.items() if not k.startswith('_')}
    print(f'재탐색 완료 {len(targets)}건 — {by_status}')


if __name__ == '__main__':
    main()
