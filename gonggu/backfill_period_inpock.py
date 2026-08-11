#!/usr/bin/env python3
"""보강(인포크 우선): 캡션에 기간이 없어 상품 gonggu_stage='판단불가'로 남은 상품의 공구기간을,
그 상품이 실린 포스트의 **인포크 허브 파싱본**(data/linkbio/<날짜>.jsonl)에서 찾아 채운다.

배경: 공구기간은 몰 상품페이지엔 잘 없고 캡션/인포크에 있다 — 예고 달력(뮤즈마켓 등)처럼 상품별
기간이 인포크 링크 제목/텍스트에 박혀 있다. resolve가 인포크 파싱본을 게시일별 JSON으로 남기므로,
그 텍스트를 기간 추출 LLM(PERIOD_BACKFILL_SYSTEM — 몰 페이지용과 동일, page_text에 인포크 텍스트를
넣을 뿐)에 태워 상품별 기간을 찾는다. 크롤링이 없어 빠르고 안티봇도 안 탄다.

데일리에서는 backfill_period(몰 크롤)보다 **먼저** 돌려, 인포크로 찾을 수 있는 건 여기서 채우고
남은 것만 몰 크롤로 넘긴다 → 몰 크롤 부담·429가 크게 준다.

⚠ 기존 값 불가침: 대상은 상품 stage='판단불가'(시작/종료일 NULL)뿐 — 이미 날짜 있는 상품은
조회조차 안 돼 덮어쓸 여지가 없다. 찾으면 그 상품의 gonggu_start/end/stage만 UPDATE(링크 불가침).
몰 크롤 backfill과 달리 link_status='done'을 요구하지 않는다(인포크는 링크 확정과 무관).

체크포인트(data/output/period_backfill_inpock.jsonl): found/not_found 모두 기록하고 재실행 시
스킵(같은 인포크 텍스트를 매번 다시 LLM 태우지 않음). 인포크 덤프가 더 쌓인 뒤 재시도하려면
체크포인트 파일을 지우면 된다.

사용법:
    python3 -m gonggu.backfill_period_inpock
    LIMIT=20 python3 -m gonggu.backfill_period_inpock
    CONCURRENCY=8 python3 -m gonggu.backfill_period_inpock
"""
import os
import sys

from gonggu.common import (DEEPSEEK_KEY, ROOT, acquire_lock, append_jsonl, call_llm, connect_dst,
                           load_jsonl)
from gonggu.crawl_linkbio import OUT_DIR as LINKBIO_DIR
from gonggu.llm_batch import retry_llm, run_llm_batch
from gonggu.platforms import PLATFORMS, product_update_period_sql
from gonggu.prompts import PERIOD_BACKFILL_SYSTEM, build_period_backfill_user
from gonggu.transform import _compute_stage, _valid_date

CHECKPOINT_FILE = ROOT / 'data/output/period_backfill_inpock.jsonl'
UPDATE_SQL = {code: product_update_period_sql(p) for code, p in PLATFORMS.items()}
PAGE_TEXT_LIMIT = int(os.environ.get('INPOCK_TEXT_LIMIT', '4000'))


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
                out[(code, pid)] = (out.get((code, pid), '') + '\n' + blob).strip()[:PAGE_TEXT_LIMIT]
    return out


def _select_sql(p):
    """상품 stage='판단불가'인 상품(link_status 무관 — 인포크는 링크 확정과 별개). 기간이 상품
    단위로 이전됨(2026-08-06)."""
    parent_cols = ', '.join(f'p.{c}' for c in p.parent_ctx_cols)
    return f"""
SELECT pp.id AS row_id, pp.product_name, {parent_cols}, p.classification_note
FROM {p.product_table} pp
JOIN {p.parent_table} p ON p.{p.id_col} = pp.{p.id_col}
WHERE pp.gonggu_stage = '판단불가'
"""


def main():
    acquire_lock('backfill_period_inpock')
    if not DEEPSEEK_KEY:
        print('.env에 DEEPSEEK_KEY가 필요합니다.', file=sys.stderr)
        sys.exit(1)

    inpock = _load_inpock_texts()
    print(f'인포크 덤프에서 텍스트 확보: {len(inpock)}개 포스트')

    conn = connect_dst()
    try:
        raw = []
        with conn.cursor() as cur:
            for code, p in PLATFORMS.items():
                cur.execute(_select_sql(p))
                for r in cur.fetchall():
                    nid = r[p.id_col]
                    raw.append((code, f'{code}:{nid}#{r["row_id"]}', nid, r, str(r[p.date_col])))
    finally:
        conn.close()

    checkpoint = load_jsonl(CHECKPOINT_FILE)
    # 인포크 텍스트가 있는 판단불가 상품만 대상(없으면 여기서 도울 수 없음) + 체크포인트 미기록만.
    todo = [t for t in raw if (t[0], t[2]) in inpock and t[1] not in checkpoint]
    no_inpock = sum(1 for t in raw if (t[0], t[2]) not in inpock)
    limit = int(os.environ.get('LIMIT', '0')) or len(todo)
    todo = todo[:limit]
    print(f'판단불가 상품 {len(raw)}건 중 인포크 텍스트 있는 미처리 {len(todo)}건 실행 '
          f'(인포크 없음 {no_inpock}건, 체크포인트 스킵 {sum(1 for t in raw if t[1] in checkpoint)}건, '
          f'동시 {int(os.environ.get("CONCURRENCY", "4"))})')
    if not todo:
        print('  인포크로 채울 판단불가 상품이 없습니다.')
        return

    def process_one(t):
        code, key, nid, r, pubdate = t
        text = inpock[(code, nid)]
        parsed, err = retry_llm(lambda: call_llm(
            PERIOD_BACKFILL_SYSTEM,
            build_period_backfill_user(r['product_name'], r.get('classification_note'), pubdate, text)))
        if err or not parsed:
            return {'code': code, 'key': key, 'row_id': r['row_id'], 'error': err or 'LLM 실패',
                    'start': None, 'end': None, 'note': (err or 'LLM 실패')[:120]}
        start = _valid_date(parsed.get('period_start'))
        end = _valid_date(parsed.get('period_end'))
        return {'code': code, 'key': key, 'row_id': r['row_id'], 'error': None,
                'start': start, 'end': end, 'note': (parsed.get('reason') or '')[:120]}

    def persist(res):
        found = bool(res['start'] or res['end'])
        if found and not res['error']:
            dst = persist.conn
            dst.ping(reconnect=True)
            with dst.cursor() as cur:
                cur.execute(UPDATE_SQL[res['code']],
                            (res['start'], res['end'], _compute_stage(res['start'], res['end']), res['row_id']))
            dst.commit()
        append_jsonl(CHECKPOINT_FILE,
                     {'key': res['key'], 'status': 'found' if found else 'not_found',
                      'period_start': res['start'], 'period_end': res['end'], 'note': res['note']})

    persist.conn = connect_dst()
    try:
        counters = run_llm_batch(todo, process_one, persist,
                                 concurrency=int(os.environ.get('CONCURRENCY', '4')),
                                 error_of=lambda r: r['error'])
    finally:
        persist.conn.close()

    cp = load_jsonl(CHECKPOINT_FILE)
    found = sum(1 for v in cp.values() if v.get('status') == 'found')
    print(f'완료 — 이번 배치 성공 {counters["ok"]} / 실패 {counters["err"]}, 누적 기간 확정 {found}건')


if __name__ == '__main__':
    main()
