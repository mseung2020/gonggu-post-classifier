#!/usr/bin/env python3
"""보강: 캡션에 공구기간이 없어 상품 gonggu_stage='판단불가'로 남은 상품의 기간을 찾아 채운다.
2026-08-18 병합(문제 10) — 원래 별도 스크립트였던 backfill_period_inpock(인포크 텍스트, 크롤
없음)과 backfill_period(몰 크롤)를 하나의 2단 에스컬레이션으로 합쳤다. 공구기간은 몰
상품페이지엔 잘 없고 캡션/인포크에 있는 경우가 많아서, 크롤 없이 훨씬 싸게 찾을 수 있는
인포크 텍스트부터 먼저 시도하고, 그래도 못 찾았고 링크가 이미 확정된 상품만 실제 상품페이지를
크롤링한다(resolve_links의 Tier0/Tier1 브라우저 없는 빠른 패스와 같은 패턴,
crawl_pool.run_crawl_pool의 use_playwright 참고):

  - **Tier0(인포크)**: 그 포스트의 인포크 허브 파싱본(data/linkbio/<날짜>.jsonl, resolve가 남김)에
    기간이 있을 만한 텍스트가 있으면 크롤 없이 LLM(PERIOD_BACKFILL_SYSTEM)에 태운다. 찾으면 즉시
    확정. 못 찾았거나 인포크 텍스트 자체가 없으면 Tier1로 넘어간다.
  - **Tier1(몰 크롤)**: 그 상품의 링크가 이미 link_status='done'으로 확정된 경우에만, 그 확정
    상품페이지를 크롤링해서 같은 LLM으로 다시 찾는다.
  - 둘 다 시도할 게 없으면(인포크 텍스트도 없고 아직 link_status='done'도 아니면) 이번 실행에서는
    아예 건드리지 않는다(재시도 횟수도 안 늘림 — 검사할 소스 자체가 없었으므로).

병합 전에는 인포크 쪽 체크포인트가 "한 번 못 찾으면 영구 스킵"(파일을 수동으로 지워야 재시도)
이었는데, 몰 크롤 쪽은 "쿨다운 후 재시도, MAX_ATTEMPTS 넘으면 영구 스킵"이었다 — 서로 다른
정책이 같은 목적("판단불가 채우기")에 붙어있는 게 일관성이 없었다. 병합 후에는 **하나의
체크포인트/재시도 정책**(아래)만 쓴다. 부작용: 옛 인포크 전용 체크포인트(data/output/
period_backfill_inpock.jsonl)는 더 이상 참조하지 않으므로, 거기서 이미 "영구 실패"로
기록됐던 상품들도 새 정책(쿨다운+상한) 아래서 한 번씩 다시 기회를 얻는다 — 의도된 재검토다.

⚠ 보수적 원칙(기존 데이터 절대 훼손 안 함):
- 대상 자체가 상품 stage='판단불가'(그 상품의 gonggu_start_date/end_date 둘 다 NULL)뿐이라, 이미
  날짜가 있는 상품은 조회조차 되지 않는다 — 기존 값을 덮어쓸 여지가 구조적으로 없다.
- 기간/스테이지가 상품 단위로 이전됨(2026-08-06) → 상품마다 개별 기간을 갖는다. 예고 달력처럼
  다중상품인 게시물의 각 상품도 자기 확정 페이지/인포크 텍스트에서 기간을 따로 찾는다.
- candidate_url(Tier1)은 링크인바이오 허브가 아니라 resolve_links가 이미 "이 상품이 맞다"고
  LLM#3로 검증한 최종 상품페이지다(link_status='done'이 되는 순간 candidate_url이 final_url로
  교체됨 — resolve_links/core.py 참고). 허브 페이지보다 오매칭 위험이 훨씬 적어서 이 페이지를 쓴다.
- LLM에게도 "이 상품명과 명백히 같은 상품을 가리키는 문구의 기간만" 인정하라고 명시한다 —
  페이지/인포크 텍스트에도 다른 상품/프로모션 정보가 같이 있을 수 있어서다.

체크포인트(data/output/period_backfill.jsonl)로 재시도를 제한한다:
- 기간을 찾았으면(found) 영구 스킵.
- 못 찾았으면(not_found) PERIOD_RETRY_COOLDOWN_DAYS일 쿨다운 후 재시도, PERIOD_MAX_ATTEMPTS회
  넘으면 영구 스킵 — 인포크 텍스트/상품페이지에 기간이 나중에 채워지는 경우를 놓치지 않으면서도,
  안 나오는 걸 매일 무한 재크롤링/재LLM 하는 비용이 쌓이는 걸 막는다.

update_gonggu_stage.py와 마찬가지로 transform.py의 _compute_stage를 재사용해서, 기간을 찾은
즉시 그 자리에서 gonggu_stage도 같이 갱신한다(다음 날 update_gonggu_stage.py를 기다릴 필요 없음).
워커 풀 배관은 crawl_pool.py 공용 모듈(2단계 B3 — LazyPage/MAX_BROWSERS 안전판 포함, 예전엔
이 파일만 워커 수=크롬 수로 돌던 감사 A1 사고 지점), 플랫폼별 SQL은 platforms.py(2단계 B4).

사용법:
    python3 -m gonggu.backfill_period
    LIMIT=20 python3 -m gonggu.backfill_period                 # 소규모 테스트
    PERIOD_INPOCK_CONCURRENCY=40 python3 -m gonggu.backfill_period   # Tier0(인포크, 브라우저 무관) 동시성
    BACKFILL_PERIOD_CONCURRENCY=4 python3 -m gonggu.backfill_period  # Tier1(몰 크롤) 동시성
"""
import datetime
import os
import sys

from gonggu.common import (DEEPSEEK_KEY, ROOT, acquire_lock, append_jsonl, call_llm, connect_dst,
                           load_jsonl)
from gonggu.crawl_linkbio import OUT_DIR as LINKBIO_DIR
from gonggu.crawl_pool import run_crawl_pool
from gonggu.llm_batch import retry_llm
from gonggu.platforms import PLATFORMS, product_update_period_sql
from gonggu.prompts import PERIOD_BACKFILL_SYSTEM, build_period_backfill_user
from gonggu.resolve_links.browser import fetch
from gonggu.resolve_links.config import BLOCKED_STATUS_CODES, BLOCKED_TEXT_MARKERS, MAX_BROWSERS
from gonggu.transform import _compute_stage, _valid_date

# Tier1(몰 크롤, 브라우저 필요) 동시성 — 기존 backfill_period.py와 동일 이름/기본값 유지.
CONCURRENCY = int(os.environ.get('BACKFILL_PERIOD_CONCURRENCY', '4'))
# Tier0(인포크 텍스트, 브라우저 무관) 동시성 — 기존 backfill_period_inpock.py의 CONCURRENCY와
# 같은 역할이라 daily.py 기본값(40)도 그대로 옮겨왔다. 이름을 구분해서 두 티어의 동시성을
# 헷갈리지 않게 한다(resolve_links의 RESOLVE_FAST_CONCURRENCY/RESOLVE_CONCURRENCY 구분과 같은 취지).
PERIOD_INPOCK_CONCURRENCY = int(os.environ.get('PERIOD_INPOCK_CONCURRENCY', '40'))
RETRY_COOLDOWN_DAYS = int(os.environ.get('PERIOD_RETRY_COOLDOWN_DAYS', '5'))
MAX_ATTEMPTS = int(os.environ.get('PERIOD_MAX_ATTEMPTS', '3'))
CHECKPOINT_FILE = ROOT / 'data/output/period_backfill.jsonl'
INPOCK_TEXT_LIMIT = int(os.environ.get('INPOCK_TEXT_LIMIT', '4000'))

UPDATE_SQL = {code: product_update_period_sql(p) for code, p in PLATFORMS.items()}


def _inpock_text(d):
    """인포크 파싱 dict에서 기간이 있을 만한 텍스트를 모아 한 덩어리로 만든다 — 제목/소개/공지/
    텍스트블록 + 링크·스토어·컬렉션의 제목과 상품명. 기간 문구는 대개 링크 제목("OPEN 8.7~8.10
    [상품명]")이나 텍스트블록에 들어있다."""
    if not isinstance(d, dict):
        return ''
    parts = [d.get('title'), d.get('bio'), d.get('notice')]
    parts += d.get('texts') or []
    for l in d.get('links') or []:
        parts.append(l.get('title'))
    for s in d.get('smart_stores') or []:
        parts.append(s.get('title'))
        parts += [pr.get('name') for pr in s.get('products') or []]
    for c in d.get('collections') or []:
        parts.append(c.get('title'))
        parts += [pr.get('name') for pr in c.get('products') or []]
    return '\n'.join(x for x in parts if x)


def _load_inpock_texts():
    """data/linkbio/<날짜>.jsonl 전부 읽어 {(code, native_id): 인포크 텍스트}로. _hub_cache 등
    밑줄 파일은 제외. 한 포스트에 허브가 여러 개면 텍스트를 합친다."""
    out = {}
    if not LINKBIO_DIR.exists():
        return out
    for f in sorted(LINKBIO_DIR.glob('*.jsonl')):
        if f.name.startswith('_'):
            continue
        for rec in load_jsonl(f).values():
            code, pid = rec.get('platform'), rec.get('post_id')
            if not pid:
                continue
            blob = '\n'.join(_inpock_text(lb.get('parsed'))
                             for lb in rec.get('linkbio') or [] if lb.get('parsed'))
            blob = blob.strip()
            if blob:
                out[(code, pid)] = (out.get((code, pid), '') + '\n' + blob).strip()[:INPOCK_TEXT_LIMIT]
    return out


def _select_sql(p):
    """상품 stage='판단불가'인 상품 전체(link_status 무관) — Tier0(인포크)는 링크 확정과
    무관하게 시도할 수 있고, Tier1(몰 크롤) 자격은 코드에서 link_status로 개별 판단한다.
    기간이 상품 단위로 이전됨(2026-08-06)."""
    parent_cols = ', '.join(f'p.{c}' for c in p.parent_ctx_cols)
    return f"""
SELECT pp.id AS row_id, pp.product_name, pp.candidate_url, pp.link_status, {parent_cols},
       p.classification_note
FROM {p.product_table} pp
JOIN {p.parent_table} p ON p.{p.id_col} = pp.{p.id_col}
WHERE pp.gonggu_stage = '판단불가'
"""


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
    """체크포인트 기록을 보고 이번 실행 대상에서 뺄지 결정한다(Tier0/Tier1 공통, 어느 티어에서
    나온 기록이든 정책은 하나)."""
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


def _has_any_source(inpock, code, r):
    """이번 실행에서 시도해볼 소스가 하나라도 있는지 — 인포크 텍스트도 없고 링크도 아직
    확정 전이면 검사 자체가 불가능하므로, 재시도 횟수를 늘리지 않고 조용히 건너뛴다."""
    nid = r[PLATFORMS[code].id_col]
    return (code, nid) in inpock or r['link_status'] == 'done'


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


def _ask_llm(text, product_name, classification_note, publish_date):
    """PERIOD_BACKFILL_SYSTEM 호출 + 결과 파싱 — Tier0(인포크 텍스트)/Tier1(크롤 본문) 공용.
    반환: (start, end, note)."""
    parsed, err = retry_llm(lambda: call_llm(
        PERIOD_BACKFILL_SYSTEM,
        build_period_backfill_user(product_name, classification_note, publish_date, text)))
    if err or not parsed:
        return None, None, (err or 'LLM 실패')
    return (_valid_date(parsed.get('period_start')), _valid_date(parsed.get('period_end')),
            parsed.get('reason') or '')


def main():
    acquire_lock('backfill_period')
    if not DEEPSEEK_KEY:
        print('.env에 DEEPSEEK_KEY가 필요합니다.', file=sys.stderr)
        sys.exit(1)

    inpock = _load_inpock_texts()
    print(f'인포크 덤프에서 텍스트 확보: {len(inpock)}개 포스트')

    conn = connect_dst()
    try:
        raw_targets = _fetch_targets(conn)
    finally:
        conn.close()

    checkpoint = load_jsonl(CHECKPOINT_FILE)
    today = datetime.date.today()
    eligible = [t for t in raw_targets if not _should_skip(checkpoint.get(t[1]), today)]
    checkpoint_skipped = len(raw_targets) - len(eligible)

    actionable = [t for t in eligible if _has_any_source(inpock, t[0], t[2])]
    no_source_skipped = len(eligible) - len(actionable)

    limit = int(os.environ.get('LIMIT', '0')) or len(actionable)
    targets = actionable[:limit]
    limit_skipped = len(actionable) - len(targets)
    print(f'판단불가 상품 {len(raw_targets)}건 중 이번 실행 {len(targets)}건 '
          f'(체크포인트로 스킵 {checkpoint_skipped}건, 검사할 소스 없음(인포크 텍스트 없음+'
          f'링크 미확정) {no_source_skipped}건, LIMIT으로 보류 {limit_skipped}건)')
    if not targets:
        return

    counters = {}
    needs_crawl = []

    def _finish(ctx, code, key, row_id, start, end, note, tier):
        found = bool(start or end)
        prev = checkpoint.get(key)
        attempts = (prev.get('attempts', 0) if prev else 0) + 1
        result = {'key': key, 'status': 'found' if found else 'not_found',
                  'checked_at': datetime.date.today().isoformat(), 'attempts': attempts,
                  'period_start': start, 'period_end': end, 'note': (note or '')[:200], 'tier': tier}
        with ctx.lock:
            if found:
                stage = _compute_stage(start, end)
                db = ctx.state
                db.ping(reconnect=True)
                with db.cursor() as cur:
                    cur.execute(UPDATE_SQL[code], (start, end, stage, row_id))
                db.commit()
            checkpoint[key] = result
            append_jsonl(CHECKPOINT_FILE, result)
            counters[result['status']] = counters.get(result['status'], 0) + 1
            counters['_done_n'] = counters.get('_done_n', 0) + 1
            print(f"  [{counters['_done_n']}/{len(targets)}] (w{ctx.worker_id}) [{tier}] {key} -> "
                  f"{result['status']} {start or ''}~{end or ''} {(note or '')[:60]}", flush=True)

    def handle_tier0(ctx, target):
        """Tier0: 인포크 텍스트만(브라우저 없음). 못 찾았고 몰 크롤도 가능하면 Tier1로 넘긴다."""
        code, key, r = target
        p = PLATFORMS[code]
        nid = r[p.id_col]
        text = inpock.get((code, nid))
        note = None
        if text:
            start, end, note = _ask_llm(text, r['product_name'], r.get('classification_note'),
                                        str(r[p.date_col]))
            if start or end:
                _finish(ctx, code, key, r['row_id'], start, end, note, 'inpock')
                return
        if r['link_status'] == 'done':
            with ctx.lock:
                needs_crawl.append((code, key, r))
            return
        _finish(ctx, code, key, r['row_id'], None, None,
                note or '인포크 텍스트 없음/링크 미확정이라 몰 크롤 대상도 아님', 'inpock')

    def handle_tier1(ctx, target):
        """Tier1: link_status='done'인 확정 상품페이지를 크롤링(브라우저 필요)."""
        code, key, r = target
        p = PLATFORMS[code]
        try:
            page_text = _page_text_or_raise(ctx.page, r['candidate_url'])
            start, end, note = _ask_llm(page_text, r['product_name'], r.get('classification_note'),
                                        str(r[p.date_col]))
        except Exception as e:
            start = end = None
            note = f'크롤링/LLM 실패: {str(e)[:160]}'
        _finish(ctx, code, key, r['row_id'], start, end, note, 'mall')

    print(f'  — Tier0(인포크 텍스트, 크롤 없음) 동시 {PERIOD_INPOCK_CONCURRENCY}개, '
          f'Tier1(몰 크롤) 동시 워커 {CONCURRENCY}개/브라우저 상한 {MAX_BROWSERS}개')
    run_crawl_pool(targets, handle_tier0, concurrency=PERIOD_INPOCK_CONCURRENCY, item_delay=0,
                   worker_setup=connect_dst, worker_teardown=lambda db: db.close(),
                   use_playwright=False)

    if needs_crawl:
        print(f'[Tier1: 몰 크롤] {len(needs_crawl)}건')
        run_crawl_pool(needs_crawl, handle_tier1, concurrency=CONCURRENCY, item_delay=0,
                       worker_setup=connect_dst, worker_teardown=lambda db: db.close(),
                       warn_hint='BACKFILL_PERIOD_CONCURRENCY')
    else:
        print('[Tier1 생략] Tier0(인포크)에서 다 끝났거나, 넘길 대상이 없음')

    by_status = {k: v for k, v in counters.items() if not k.startswith('_')}
    print(f'기간 백필 완료 {len(targets)}건 — {by_status}')


if __name__ == '__main__':
    main()
