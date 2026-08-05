#!/usr/bin/env python3
"""5단계 보강: gonggu_stage='진행중'인데 link_status가 'unresolved'/'hold'인 상품만 골라 링크
해석을 다시 시도한다. 시작전 단계에서는 인포크 등에 아직 구매 링크가 없어서 못 찾았을 뿐인데,
진행중이 되면 실제로 링크가 채워지는 경우가 많아서(원준님 피드백 반영) 이 전이 시점을
노려 재탐색한다. hold도 unresolved와 동일하게 취급한다(2026-07-29 결정) — hold도 결국
candidate_url이 허브 URL 하나로 남아있는 경우가 많아서 재확인할 가치가 같다.

link_status='error'는 위 두 상태와 성격이 달라 조건을 따로 둔다(2026-08-04 추가) — unresolved/
hold는 "아직 링크가 없어서" 못 찾은 것이라 진행중 전환이라는 사업적 신호를 기다릴 이유가
있지만, error는 크롤링/LLM 호출 자체가 실패한 순수 기술적 문제(예: DeepSeek 타임아웃 폭주)라
gonggu_stage와 무관하게 재시도할 가치가 있다. 그래서 error는 진행중 여부를 안 따지고 무조건
대상에 포함한다.

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
    RESCAN_SKIP_TODAY=0 python3 scripts/rescan_inprogress.py   # 하루 제한 무시하고 강제로 전체 재시도

RESCAN_CONCURRENCY는 "동시에 처리 중인 상품 수"지 "동시에 뜨는 크롬 수"가 아니다 — 실제
브라우저 개수는 MAX_BROWSERS가 따로 제한한다(resolve_links/browser.py의 LazyPage). 다만
브라우저가 필요한 건의 처리량은 결국 MAX_BROWSERS가 정하므로, 워커만 무작정 올려도 그만큼
빨라지지는 않는다(둘의 차이가 3배를 넘으면 실행 시작 시 경고가 뜬다).

RESCAN_SKIP_TODAY(기본 1) — 오늘 날짜 안에 이미 한 번 손댄(updated_at) 행은 대상에서 뺀다
(2026-08-04 추가). 자정이 지나면 날짜가 바뀌어 자동으로 초기화된다 — 분 단위 쿨다운이 아니라
"하루에 한 번"이라 계산이 단순하고, 오늘 도중에 이 스크립트를 몇 번을 중간에 멈추고 다시
돌려도(예: DeepSeek 503 폭주 중 재시작 반복) 오늘 이미 시도했던 건은 다시 안 건드린다. 별도
체크포인트 파일 없이 이미 있는 updated_at 컬럼(ON UPDATE CURRENT_TIMESTAMP)만 쓴다. 오늘 안에
꼭 다시 시도해야 하면 RESCAN_SKIP_TODAY=0으로 끄고 돌릴 것.
"""
import datetime
import os
import queue
import sys
import threading
import time

from playwright.sync_api import sync_playwright

from gonggu.common import DEEPSEEK_KEY, append_jsonl, connect_dst
from gonggu.resolve_links.browser import LazyPage
from gonggu.resolve_links.config import HTTP_FAST_PATH, ITEM_DELAY, MAX_BROWSERS, RESOLUTION_FILE
from gonggu.resolve_links.core import resolve_product
from gonggu.resolve_links.httpfetch import stats as httpfetch_stats
from gonggu.resolve_links.matching import product_key

RESCAN_CONCURRENCY = int(os.environ.get('RESCAN_CONCURRENCY', '4'))
RESCAN_SKIP_TODAY = os.environ.get('RESCAN_SKIP_TODAY', '1') != '0'


def _cutoff_ts():
    """RESCAN_SKIP_TODAY가 켜져 있으면 '오늘 00:00'을 돌려준다 — updated_at이 이보다 이르면
    (=오늘 안 건드림) 대상에 포함(조건은 `updated_at < cutoff_ts`). 꺼져 있으면 사실상 모든
    행이 조건을 통과하도록 아주 먼 미래 시각을 돌려준다(과거를 주면 정반대로 거의 아무것도
    안 걸림 — 2026-08-04에 실측으로 잡은 버그). MySQL의 CURDATE()/NOW() 대신 파이썬의 오늘
    날짜를 기준으로 계산해서 나머지 파이프라인(transform.py의 _compute_stage 등)과 같은
    "오늘"의 기준을 쓴다."""
    if not RESCAN_SKIP_TODAY:
        return datetime.datetime(9999, 12, 31)
    today = datetime.date.today()
    return datetime.datetime(today.year, today.month, today.day)


SELECT_POST = """
SELECT pp.id AS row_id, pp.product_name, pp.link_location, pp.url_type, pp.candidate_url,
       pp.sort_order, p.post_id, p.user_id, p.url, p.publish_date, p.classification_note
FROM gonggu_post_product pp
JOIN gonggu_post p ON p.post_id = pp.post_id
WHERE ((p.gonggu_stage = '진행중' AND pp.link_status IN ('unresolved', 'hold'))
   OR pp.link_status = 'error')
  AND pp.updated_at < %s
"""

SELECT_VIDEO = """
SELECT vp.id AS row_id, vp.product_name, vp.link_location, vp.url_type, vp.candidate_url,
       vp.sort_order, v.video_id, v.channel_id, v.video_url, v.publishDate, v.classification_note
FROM gonggu_video_product vp
JOIN gonggu_video v ON v.video_id = vp.video_id
WHERE ((v.gonggu_stage = '진행중' AND vp.link_status IN ('unresolved', 'hold'))
   OR vp.link_status = 'error')
  AND vp.updated_at < %s
"""

# updated_at을 ON UPDATE CURRENT_TIMESTAMP 자동 트리거에만 맡기지 않고 여기서 NOW()로 직접
# 강제 갱신한다(2026-08-05 추가) — MySQL은 UPDATE 문이 실행돼도 값이 실제로 하나도 안 바뀌면
# 자동 트리거를 안 태운다. "크롤링할 후보 링크 없음"처럼 이번에도 똑같이 candidate_url=NULL,
# link_status='unresolved'로 끝나는 항목은 값이 그대로라 updated_at이 안 갱신되고, 그러면
# RESCAN_SKIP_TODAY가 "오늘 안 건드림"으로 착각해서 매번 다시 대상에 잡힌다(실측 확인 —
# 실패가 반복되는 항목일수록 이 버그에 잘 걸림).
UPDATE_POST = 'UPDATE gonggu_post_product SET candidate_url = %s, link_status = %s, updated_at = NOW() WHERE id = %s'
UPDATE_VIDEO = 'UPDATE gonggu_video_product SET candidate_url = %s, link_status = %s, updated_at = NOW() WHERE id = %s'


def _fetch_targets(conn, cutoff_ts):
    targets = []
    with conn.cursor() as cur:
        cur.execute(SELECT_POST, (cutoff_ts,))
        for r in cur.fetchall():
            parent = {'post_id': r['post_id'], 'user_id': r['user_id'], 'url': r['url'],
                      'publish_date': str(r['publish_date']), 'classification_note': r['classification_note']}
            product = {'product_name': r['product_name'], 'link_location': r['link_location'],
                       'url_type': r['url_type'], 'candidate_url': r['candidate_url'],
                       'sort_order': r['sort_order']}
            targets.append(('ig', parent, product, r['row_id']))

        cur.execute(SELECT_VIDEO, (cutoff_ts,))
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

                # 이 블록에서 뭐가 터지든(DB 접속 끊김, 예상 못 한 None 등) 이 상품 처리만
                # 실패로 남기고 워커 스레드는 죽지 않게 한다 — 예전엔 여기서 예외가 나면
                # 스레드 자체가 죽어서 work_q에 남은 나머지 물량을 다시는 안 가져가고,
                # 그만큼 실제 동시성이 조용히 줄어들었다(실측 확인, 2026-08-04 —
                # candidate_url이 원래부터 DB에도 없고 이번에도 못 찾은 상품에서
                # `None or None`을 슬라이싱하다 TypeError로 스레드 하나가 죽음).
                try:
                    key = product_key(platform, parent, product['sort_order'])
                    update_sql = UPDATE_POST if platform == 'ig' else UPDATE_VIDEO
                    # candidate_url은 상태와 무관하게 항상 단일 URL이길 기대하지만(2026-07-29
                    # 결정), 애초에 후보 자체가 없어서 res도 product도 둘 다 None인 경우가
                    # 있을 수 있다 — 그럴 땐 값을 지어내지 말고 그대로 NULL로 둔다.
                    candidate_url = res.get('candidate_url') or product['candidate_url']
                    new_candidate_url = candidate_url[:500] if candidate_url else None

                    with lock:
                        # 크롤링/LLM 처리(수 초~수십 초)가 끝난 뒤에야 이 커넥션을 다시 쓰는
                        # 구조라, 그 사이 방화벽/DB의 idle 타임아웃으로 커넥션이 끊겨 있을 수
                        # 있다(실측 확인, 2026-08-04 — RESCAN_CONCURRENCY=100에서
                        # "Lost connection to MySQL server during query"가 대량 발생, 재연결이
                        # 없어서 그 워커는 그 뒤로 크롤링만 하고 저장은 계속 실패). ping으로
                        # 끊겼으면 자동 재연결부터 하고 실행한다.
                        db.ping(reconnect=True)
                        with db.cursor() as cur:
                            cur.execute(update_sql, (new_candidate_url, res['status'], row_id))
                        db.commit()
                        append_jsonl(RESOLUTION_FILE, {**res, 'key': key})
                        counters[res['status']] = counters.get(res['status'], 0) + 1
                        counters['_done_n'] = counters.get('_done_n', 0) + 1
                        shown = res.get('final_url') or res.get('note', '')
                        print(f"  [{counters['_done_n']}/{total}] (w{worker_id}) {key} -> {res['status']} {shown[:70]}",
                              flush=True)
                except Exception as e:
                    with lock:
                        counters['error'] = counters.get('error', 0) + 1
                        counters['_done_n'] = counters.get('_done_n', 0) + 1
                        print(f"  [{counters['_done_n']}/{total}] (w{worker_id}) row_id={row_id} -> "
                              f"저장 실패(스킵): {str(e)[:120]}", flush=True)
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
        targets = _fetch_targets(conn, _cutoff_ts())
    finally:
        conn.close()

    limit = int(os.environ.get('LIMIT', '0')) or len(targets)
    targets = targets[:limit]
    skip_note = '오늘 이미 시도한 건 제외' if RESCAN_SKIP_TODAY else '하루 제한 없이 전체'
    if not targets:
        print(f'진행중+unresolved/hold 또는 error 재탐색 대상 0건 ({skip_note})')
        return

    work_q = queue.Queue()
    for t in targets:
        work_q.put(t)
    lock = threading.Lock()
    counters = {}
    n_workers = max(1, min(RESCAN_CONCURRENCY, len(targets)))
    print(f'진행중+unresolved/hold 또는 error 재탐색 대상 {len(targets)}건({skip_note}) '
          f'— 동시 워커 {n_workers}개, 브라우저 상한 {MAX_BROWSERS}개, '
          f'requests 패스트패스 {"ON" if HTTP_FAST_PATH else "OFF"}')
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
