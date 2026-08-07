#!/usr/bin/env python3
"""보강: 캡션에 공구기간이 없어 상품 gonggu_stage='판단불가'로 남은 상품 중, 그 상품의 링크가
이미 link_status='done'으로 확정된 것을 골라 그 확정 상품페이지를 크롤링해서 페이지 안에 이
상품의 공구기간이 명시되어 있는지 LLM으로 찾는다. 찾으면 그 상품의 gonggu_start_date/
gonggu_end_date와 gonggu_stage를 함께 갱신한다.

⚠ 보수적 원칙(기존 데이터 절대 훼손 안 함):
- 대상 자체가 상품 stage='판단불가'(그 상품의 gonggu_start_date/end_date 둘 다 NULL)뿐이라, 이미
  날짜가 있는 상품은 조회조차 되지 않는다 — 기존 값을 덮어쓸 여지가 구조적으로 없다.
- 기간/스테이지가 상품 단위로 이전됨(2026-08-06) → 상품마다 개별 기간을 갖는다. 예전엔 "상품이
  정확히 1개인 포스트만" 대상으로 제한했는데(기간이 포스트 단위라 다중상품에서 어느 상품 기준인지
  모호했음), 이제 상품 단위라 그 제약이 필요 없다 — 예고 달력처럼 다중상품인 게시물의 각 상품도
  자기 확정 페이지에서 기간을 따로 찾을 수 있다.
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
워커 풀 배관은 crawl_pool.py 공용 모듈(2단계 B3 — LazyPage/MAX_BROWSERS 안전판 포함, 예전엔
이 파일만 워커 수=크롬 수로 돌던 감사 A1 사고 지점), 플랫폼별 SQL은 platforms.py(2단계 B4).

사용법:
    python3 -m gonggu.backfill_period
    LIMIT=20 python3 -m gonggu.backfill_period            # 소규모 테스트
    BACKFILL_PERIOD_CONCURRENCY=4 python3 -m gonggu.backfill_period
"""
import datetime
import os
import sys

from gonggu.common import DEEPSEEK_KEY, ROOT, append_jsonl, call_llm, connect_dst, load_jsonl
from gonggu.crawl_pool import run_crawl_pool
from gonggu.platforms import PLATFORMS, product_update_period_sql
from gonggu.prompts import PERIOD_BACKFILL_SYSTEM, build_period_backfill_user
from gonggu.resolve_links.browser import fetch
from gonggu.resolve_links.config import BLOCKED_STATUS_CODES, BLOCKED_TEXT_MARKERS, MAX_BROWSERS
from gonggu.transform import _compute_stage

CONCURRENCY = int(os.environ.get('BACKFILL_PERIOD_CONCURRENCY', '4'))
RETRY_COOLDOWN_DAYS = int(os.environ.get('PERIOD_RETRY_COOLDOWN_DAYS', '5'))
MAX_ATTEMPTS = int(os.environ.get('PERIOD_MAX_ATTEMPTS', '3'))
CHECKPOINT_FILE = ROOT / 'data/output/period_backfill.jsonl'


def _select_sql(p):
    """상품 stage='판단불가' & link_status='done'인 상품. 기간이 상품 단위로 이전돼(2026-08-06)
    '상품 1개' 제약이 사라졌다 — 상품마다 개별 기간을 갖기 때문. 테이블/컬럼명은 platforms.py에서."""
    parent_cols = ', '.join(f'p.{c}' for c in p.parent_ctx_cols)
    return f"""
SELECT pp.id AS row_id, pp.product_name, pp.candidate_url, {parent_cols}, p.classification_note
FROM {p.product_table} pp
JOIN {p.parent_table} p ON p.{p.id_col} = pp.{p.id_col}
WHERE pp.gonggu_stage = '판단불가' AND pp.link_status = 'done'
"""


UPDATE_SQL = {code: product_update_period_sql(p) for code, p in PLATFORMS.items()}


def _fetch_targets(conn):
    targets = []
    with conn.cursor() as cur:
        for code, p in PLATFORMS.items():
            cur.execute(_select_sql(p))
            for r in cur.fetchall():
                # 체크포인트 key는 상품 단위(native_id#row_id) — 같은 포스트라도 상품마다 따로.
                targets.append((code, f"{code}:{r[p.id_col]}#{r['row_id']}", r))
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
    print(f'판단불가 상품(link_status=done) 대상 {len(raw_targets)}건 중 이번 실행 {len(targets)}건 '
          f'(체크포인트로 스킵 {checkpoint_skipped}건, LIMIT으로 보류 {limit_skipped}건, '
          f'동시 워커 상한 {CONCURRENCY}개, 브라우저 상한 {MAX_BROWSERS}개)')
    if not targets:
        return

    counters = {}

    def handle(ctx, target):
        platform, key, r = target
        db = ctx.state  # 워커당 DB 커넥션 1개
        p = PLATFORMS[platform]
        try:
            page_text = _page_text_or_raise(ctx.page, r['candidate_url'])
            publish_date = str(r[p.date_col])
            verdict = call_llm(
                PERIOD_BACKFILL_SYSTEM,
                build_period_backfill_user(r['product_name'], r['classification_note'],
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

        with ctx.lock:
            if found:
                stage = _compute_stage(start, end)
                db.ping(reconnect=True)
                with db.cursor() as cur:
                    cur.execute(UPDATE_SQL[platform], (start, end, stage, r['row_id']))
                db.commit()
            checkpoint[key] = result
            append_jsonl(CHECKPOINT_FILE, result)
            counters[result['status']] = counters.get(result['status'], 0) + 1
            counters['_done_n'] = counters.get('_done_n', 0) + 1
            print(f"  [{counters['_done_n']}/{len(targets)}] (w{ctx.worker_id}) {key} -> {result['status']} "
                  f"{start or ''}~{end or ''} {note[:60]}", flush=True)

    run_crawl_pool(targets, handle, concurrency=CONCURRENCY, item_delay=0,
                   worker_setup=connect_dst, worker_teardown=lambda db: db.close(),
                   warn_hint='BACKFILL_PERIOD_CONCURRENCY')

    by_status = {k: v for k, v in counters.items() if not k.startswith('_')}
    print(f'기간 백필 완료 {len(targets)}건 — {by_status}')


if __name__ == '__main__':
    main()
