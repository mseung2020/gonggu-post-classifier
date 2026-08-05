#!/usr/bin/env python3
"""보강: 캡션에 공구기간이 없어 gonggu_stage='판단불가'로 남은 건 중, 상품이 정확히 1개이고
그 상품의 링크가 이미 link_status='done'으로 확정된 것만 골라 그 확정 상품페이지를 크롤링해서
페이지 안에 이 상품의 공구기간이 명시되어 있는지 LLM으로 찾는다. 찾으면 gonggu_start_date/
gonggu_end_date와 gonggu_stage를 함께 갱신한다.

⚠ 보수적 원칙(기존 데이터 절대 훼손 안 함):
- 대상 자체가 '판단불가'(gonggu_start_date/end_date 둘 다 NULL)뿐이라, 이미 날짜가 하나라도
  있는 행(시작전/진행중/종료)은 조회조차 되지 않는다 — 기존 값을 덮어쓸 여지가 구조적으로 없다.
- 상품이 2개 이상인 포스트/영상은 스코프 밖이다. gonggu_start_date/end_date는 상품이 아니라
  포스트/영상(parent) 단위 컬럼인데, 상품이 여럿이면 원칙적으로 "정말 서로 무관한 공구가
  병렬로 나열된" 경우라(README 참고) 그중 한 상품 페이지의 기간을 포스트 전체 기간에 넣는 게
  개념적으로 틀리기 때문이다.
- candidate_url은 링크인바이오 허브가 아니라 resolve_links가 이미 "이 상품이 맞다"고 LLM#3로
  검증한 최종 상품페이지다(link_status='done'이 되는 순간 candidate_url이 final_url로 교체됨 —
  resolve_links/core.py 참고). 허브 페이지보다 오매칭 위험이 훨씬 적어서 이 페이지를 쓴다.
- LLM에게도 "이 상품명과 명백히 같은 상품을 가리키는 문구의 기간만" 인정하라고 명시한다 —
  상품페이지에도 다른 상품/프로모션 배너가 같이 있을 수 있어서다.

체크포인트(data/output/period_backfill.jsonl)로 재시도를 제한한다:
- 기간을 찾았으면(found) 영구 스킵.
- 못 찾았으면(not_found) PERIOD_RETRY_COOLDOWN_DAYS일 쿨다운 후 재시도, PERIOD_MAX_ATTEMPTS회
  넘으면 영구 스킵 — 상품페이지에 기간이 나중에 채워지는 경우를 놓치지 않으면서도, 안 나오는
  페이지를 매일 무한 재크롤링/재LLM 하는 비용이 쌓이는 걸 막는다.

update_gonggu_stage.py와 마찬가지로 transform.py의 _compute_stage를 재사용해서, 기간을 찾은
즉시 그 자리에서 gonggu_stage도 같이 갱신한다(다음 날 update_gonggu_stage.py를 기다릴 필요 없음).

사용법:
    python3 scripts/backfill_period.py
    LIMIT=20 python3 scripts/backfill_period.py            # 소규모 테스트
    BACKFILL_PERIOD_CONCURRENCY=4 python3 scripts/backfill_period.py
"""
import datetime
import os
import queue
import sys
import threading

from playwright.sync_api import sync_playwright

from gonggu.common import DEEPSEEK_KEY, ROOT, append_jsonl, call_llm, connect_dst, load_jsonl
from gonggu.prompts import PERIOD_BACKFILL_SYSTEM, build_period_backfill_user
from gonggu.resolve_links.browser import LazyPage, fetch
from gonggu.resolve_links.config import BLOCKED_STATUS_CODES, BLOCKED_TEXT_MARKERS, MAX_BROWSERS
from gonggu.transform import _compute_stage

CONCURRENCY = int(os.environ.get('BACKFILL_PERIOD_CONCURRENCY', '4'))
RETRY_COOLDOWN_DAYS = int(os.environ.get('PERIOD_RETRY_COOLDOWN_DAYS', '5'))
MAX_ATTEMPTS = int(os.environ.get('PERIOD_MAX_ATTEMPTS', '3'))
CHECKPOINT_FILE = ROOT / 'data/output/period_backfill.jsonl'

# 상품이 정확히 1개인 포스트/영상만 대상으로 한다(위 docstring 참고) — 서브쿼리로 그 포스트/
# 영상에 달린 상품 총개수를 세서 1인 것만 남긴다.
SELECT_POST = """
SELECT p.post_id, p.user_id, p.url, p.publish_date, p.classification_note,
       pp.product_name, pp.candidate_url
FROM gonggu_post p
JOIN gonggu_post_product pp ON pp.post_id = p.post_id
WHERE p.gonggu_stage = '판단불가' AND pp.link_status = 'done'
  AND (SELECT COUNT(*) FROM gonggu_post_product pp2 WHERE pp2.post_id = p.post_id) = 1
"""

SELECT_VIDEO = """
SELECT v.video_id, v.channel_id, v.video_url, v.publishDate, v.classification_note,
       vp.product_name, vp.candidate_url
FROM gonggu_video v
JOIN gonggu_video_product vp ON vp.video_id = v.video_id
WHERE v.gonggu_stage = '판단불가' AND vp.link_status = 'done'
  AND (SELECT COUNT(*) FROM gonggu_video_product vp2 WHERE vp2.video_id = v.video_id) = 1
"""

UPDATE_POST = ('UPDATE gonggu_post SET gonggu_start_date=%s, gonggu_end_date=%s, gonggu_stage=%s '
               'WHERE post_id=%s')
UPDATE_VIDEO = ('UPDATE gonggu_video SET gonggu_start_date=%s, gonggu_end_date=%s, gonggu_stage=%s '
                'WHERE video_id=%s')


def _fetch_targets(conn):
    targets = []
    with conn.cursor() as cur:
        cur.execute(SELECT_POST)
        for r in cur.fetchall():
            targets.append(('ig', f"ig:{r['post_id']}", r))
        cur.execute(SELECT_VIDEO)
        for r in cur.fetchall():
            targets.append(('yt', f"yt:{r['video_id']}", r))
    return targets


def _should_skip(rec, today):
    """체크포인트 기록을 보고 이번 실행 대상에서 뺄지 결정한다."""
    if not rec:
        return False
    if rec['status'] == 'found':
        return True
    if rec.get('attempts', 0) >= MAX_ATTEMPTS:
        return True
    checked = rec.get('checked_at')
    if checked and (today - datetime.date.fromisoformat(checked)).days < RETRY_COOLDOWN_DAYS:
        return True
    return False


def _page_text_or_raise(page, url):
    res = fetch(page, url)
    if res.get('error'):
        raise ValueError(f"크롤링 실패: {res['error']}")
    if res.get('status') in BLOCKED_STATUS_CODES:
        raise ValueError(f"로그인월_차단 추정 — HTTP {res.get('status')}")
    body_text = res.get('body_text') or ''
    if any(m.lower() in body_text.lower() for m in BLOCKED_TEXT_MARKERS):
        raise ValueError('로그인월_차단 추정 — 본문이 보안확인/캡차 문구')
    if not body_text:
        raise ValueError('본문 텍스트 없음')
    return body_text


def _worker(worker_id, work_q, checkpoint, lock, counters, total, save_auth_state):
    # DB는 워커(스레드)당 1개만 열어서 재사용한다(rescan_inprogress.py와 동일 패턴).
    db = connect_dst()
    try:
        with sync_playwright() as pw:
            # 브라우저는 LazyPage로 — 실제로 필요해지는 첫 순간까지 미루고, 동시 개수도
            # MAX_BROWSERS 허가증으로 제한한다. 예전엔 여기서 워커마다 new_context_page()를
            # 직접 불러서 사실상 "BACKFILL_PERIOD_CONCURRENCY = 크롬 프로세스 수"였다 —
            # rescan_inprogress.py는 2026-08-04에 LazyPage로 고쳐졌는데 이 파일만 누락돼
            # 있었다(2026-08-05 감사 A1). CONCURRENCY=50으로 돌리면 크롬 50개가 동시에 뜨는,
            # 2026-07-30 스왑 32GB 사고와 같은 패턴이었음. 대상 페이지는 requests 패스트패스로
            # 끝나는 비율이 높아서(fetch가 먼저 시도) 지연 생성의 이득도 크다.
            page = LazyPage(pw, save_auth_state=save_auth_state)
            while True:
                try:
                    platform, key, r = work_q.get_nowait()
                except queue.Empty:
                    break

                try:
                    page_text = _page_text_or_raise(page, r['candidate_url'])
                    publish_date = str(r.get('publish_date') or r.get('publishDate'))
                    verdict = call_llm(
                        PERIOD_BACKFILL_SYSTEM,
                        build_period_backfill_user(r['product_name'], r.get('classification_note'),
                                                    publish_date, page_text))
                    start, end, note = verdict.get('period_start'), verdict.get('period_end'), verdict.get('reason', '')
                except Exception as e:
                    start = end = None
                    note = f'크롤링/LLM 실패: {str(e)[:160]}'

                prev = checkpoint.get(key)
                attempts = (prev.get('attempts', 0) if prev else 0) + 1
                found = bool(start or end)
                result = {'key': key, 'status': 'found' if found else 'not_found',
                          'checked_at': datetime.date.today().isoformat(), 'attempts': attempts,
                          'period_start': start, 'period_end': end, 'note': (note or '')[:200]}

                with lock:
                    if found:
                        stage = _compute_stage(start, end)
                        native_id = r['post_id'] if platform == 'ig' else r['video_id']
                        update_sql = UPDATE_POST if platform == 'ig' else UPDATE_VIDEO
                        with db.cursor() as cur:
                            cur.execute(update_sql, (start, end, stage, native_id))
                        db.commit()
                    checkpoint[key] = result
                    append_jsonl(CHECKPOINT_FILE, result)
                    counters[result['status']] = counters.get(result['status'], 0) + 1
                    counters['_done_n'] = counters.get('_done_n', 0) + 1
                    print(f"  [{counters['_done_n']}/{total}] (w{worker_id}) {key} -> {result['status']} "
                          f"{start or ''}~{end or ''} {note[:60]}", flush=True)
                # 브라우저를 더 안 쓰게 됐는데 기다리는 워커가 있으면 넘겨준다(runner/rescan과 동일).
                page.release_if_contended()
            # 세션 저장은 LazyPage._teardown이 save_auth_state에 따라 닫는 시점마다 처리한다.
            page.close()
    finally:
        db.close()


def main():
    if not DEEPSEEK_KEY:
        print('.env에 DEEPSEEK_KEY가 필요합니다.', file=sys.stderr)
        sys.exit(1)

    conn = connect_dst()
    try:
        raw_targets = _fetch_targets(conn)
    finally:
        conn.close()

    checkpoint = load_jsonl(CHECKPOINT_FILE)
    today = datetime.date.today()
    eligible = [t for t in raw_targets if not _should_skip(checkpoint.get(t[1]), today)]
    checkpoint_skipped = len(raw_targets) - len(eligible)

    limit = int(os.environ.get('LIMIT', '0')) or len(eligible)
    targets = eligible[:limit]
    limit_skipped = len(eligible) - len(targets)
    print(f'판단불가 + 단일상품 + link_status=done 대상 {len(raw_targets)}건 중 이번 실행 {len(targets)}건 '
          f'(체크포인트로 스킵 {checkpoint_skipped}건, LIMIT으로 보류 {limit_skipped}건, 동시 워커 {CONCURRENCY}개, '
          f'브라우저 상한 {MAX_BROWSERS}개)')
    if not targets:
        return
    n_check = max(1, min(CONCURRENCY, len(targets)))
    if n_check > MAX_BROWSERS * 3:
        print(f'  ⚠ 워커({n_check})가 브라우저 상한({MAX_BROWSERS})의 3배를 넘습니다 — 브라우저가 '
              f'필요한 건이 많으면 재기동 오버헤드로 오히려 느려질 수 있습니다. '
              f'MAX_BROWSERS를 올리거나 BACKFILL_PERIOD_CONCURRENCY를 낮춰보세요.')

    work_q = queue.Queue()
    for t in targets:
        work_q.put(t)
    lock = threading.Lock()
    counters = {}
    n_workers = max(1, min(CONCURRENCY, len(targets)))
    threads = [
        threading.Thread(target=_worker, args=(wid, work_q, checkpoint, lock, counters, len(targets), wid == 0))
        for wid in range(n_workers)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    by_status = {k: v for k, v in counters.items() if not k.startswith('_')}
    print(f'기간 백필 완료 {len(targets)}건 — {by_status}')


if __name__ == '__main__':
    main()
