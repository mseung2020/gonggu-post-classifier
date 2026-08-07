#!/usr/bin/env python3
"""일회성: 다중상품 포스트/영상의 상품별 공구기간 백필 (기간→상품 이전 대공사, 2026-08-06).

마이그레이션(queries/migrate_period_to_product.sql)은 단일상품 포스트만 parent→product로 기간을
복사하고, **다중상품 포스트의 상품은 gonggu_stage=NULL로 남겨뒀다**(포스트 기간 하나를 여러
상품에 뭉개면 부정확하기 때문 — 예고 달력이 대표 사례). 이 스크립트가 그 다중상품들을 LLM#1
(상품별 기간을 뽑는 신 프롬프트)으로 다시 분류해 상품별 기간을 채운다.

⚠ 링크 불가침(가장 중요): 절대 상품 행을 재적재하지 않는다. LLM이 뽑은 "상품별 기간"을 기존
상품 행에 product_name으로 매칭해서 gonggu_start_date/gonggu_end_date/gonggu_stage 컬럼만
UPDATE한다. candidate_url/link_status(resolve·상세수집까지 진행된 힘들게 확정한 링크 자산)는
손도 대지 않는다. 매칭이 애매하면(이름이 0개 또는 2개 이상 걸리면) 그 상품은 건너뛴다 — NULL로
남겨두고 나중에 backfill_period가 확정 페이지에서 기간을 찾게 한다.

체크포인트(data/output/multiproduct_period_backfill.jsonl)로 재실행 안전(포스트 단위 완료 스킵).

사용법(저장소 루트에서):
    python3 -m gonggu._migrate_multiproduct_periods
    LIMIT=20 python3 -m gonggu._migrate_multiproduct_periods       # 소규모 테스트
    CONCURRENCY=8 python3 -m gonggu._migrate_multiproduct_periods
"""
import os
import re
import sys

from gonggu.common import (DEEPSEEK_KEY, ROOT, append_jsonl, call_llm, connect_dst,
                           connect_src, load_jsonl)
from gonggu.llm_batch import retry_llm, run_llm_batch
from gonggu.platforms import PLATFORMS, product_update_period_sql
from gonggu.prompts import GONGGU_CLASSIFY_SYSTEM, build_gonggu_classify_user
from gonggu.transform import _compute_stage, _valid_date

CHECKPOINT_FILE = ROOT / 'data/output/multiproduct_period_backfill.jsonl'
UPDATE_SQL = {code: product_update_period_sql(p) for code, p in PLATFORMS.items()}
_CHUNK = 300


def _norm(s):
    """상품명 매칭용 정규화 — 공백·특수문자·대소문자 차이를 지운다."""
    return re.sub(r'[^0-9a-z가-힣]', '', (s or '').lower())


def match_periods(llm_products, db_products):
    """LLM 상품[{name,period_start,period_end}] ↔ DB 상품[{id,product_name}] 매칭.
    반환: [(db_id, start, end)] — 정규화 완전일치(유일)를 우선, 없으면 부분포함(유일)만 인정.
    한 DB 상품에 LLM 후보가 0개거나 2개 이상이면 애매하므로 건너뛴다(링크 자산 보존을 위해
    보수적 — 잘못 매칭해 엉뚱한 기간을 넣느니 NULL로 두는 게 낫다)."""
    out = []
    norm_llm = [(_norm(p.get('name')), p) for p in llm_products]
    for db in db_products:
        dn = _norm(db['product_name'])
        if not dn:
            continue
        exact = [p for n, p in norm_llm if n and n == dn]
        cands = exact if exact else [p for n, p in norm_llm if n and (n in dn or dn in n)]
        if len(cands) != 1:
            continue
        lp = cands[0]
        start = _valid_date(lp.get('period_start'))
        end = _valid_date(lp.get('period_end'))
        if start or end:
            out.append((db['id'], start, end))
    return out


def _fetch_targets(dst):
    """다중상품(상품 2개 이상) 포스트/영상 → [(code, native_id, publish_date, [db_products])]."""
    targets = []
    with dst.cursor() as cur:
        for code, p in PLATFORMS.items():
            cur.execute(f"""
SELECT p.{p.id_col} AS native_id, p.{p.date_col} AS publish_date,
       pp.id AS pid, pp.product_name
FROM {p.product_table} pp
JOIN {p.parent_table} p ON p.{p.id_col} = pp.{p.id_col}
WHERE p.{p.id_col} IN (
    SELECT {p.id_col} FROM {p.product_table} GROUP BY {p.id_col} HAVING COUNT(*) >= 2)
""")
            by_post = {}
            for r in cur.fetchall():
                d = by_post.setdefault(r['native_id'],
                                       {'publish_date': r['publish_date'], 'products': []})
                d['products'].append({'id': r['pid'], 'product_name': r['product_name']})
            for nid, d in by_post.items():
                targets.append((code, nid, str(d['publish_date']), d['products']))
    return targets


def _fetch_captions(src, todo):
    """todo의 native_id들로 hifen에서 캡션 배치 조회 → {(code, native_id): caption}.
    유튜브는 classify와 동일하게 '[제목] ...' 프리픽스를 붙인다."""
    ids = {'ig': set(), 'yt': set()}
    for code, nid, _, _ in todo:
        ids[code].add(nid)
    sql = {
        'ig': 'SELECT post_id AS k, description AS caption FROM instagram_post_description WHERE post_id IN ({ph})',
        'yt': ('SELECT video_id AS k, video_description AS caption, title '
               'FROM YT_video_lists_detail WHERE video_id IN ({ph})'),
    }
    out = {}
    with src.cursor() as cur:
        for code in ('ig', 'yt'):
            id_list = sorted(ids[code])
            for i in range(0, len(id_list), _CHUNK):
                chunk = id_list[i:i + _CHUNK]
                if not chunk:
                    continue
                ph = ', '.join(['%s'] * len(chunk))
                cur.execute(sql[code].format(ph=ph), chunk)
                for r in cur.fetchall():
                    cap = r['caption'] or ''
                    if code == 'yt':
                        cap = f"[제목] {r.get('title') or ''}\n\n{cap}"
                    out[(code, r['k'])] = cap
    return out


def main():
    if not DEEPSEEK_KEY:
        print('.env에 DEEPSEEK_KEY가 필요합니다.', file=sys.stderr)
        sys.exit(1)

    dst = connect_dst()
    try:
        all_targets = _fetch_targets(dst)
        checkpoint = load_jsonl(CHECKPOINT_FILE)
        todo = [t for t in all_targets if f'{t[0]}:{t[1]}' not in checkpoint]
        limit = int(os.environ.get('LIMIT', '0')) or len(todo)
        todo = todo[:limit]
        print(f'다중상품 포스트 {len(all_targets)}개 중 이번 실행 {len(todo)}개 '
              f'(체크포인트 완료 {len(all_targets) - len([t for t in all_targets if f"{t[0]}:{t[1]}" not in checkpoint])}개 스킵)')
        if not todo:
            print('  채울 다중상품이 없습니다.')
            return

        src = connect_src()
        try:
            captions = _fetch_captions(src, todo)
        finally:
            src.close()
        print(f'  캡션 확보 {len(captions)}/{len(todo)}개, 동시 {int(os.environ.get("CONCURRENCY", "4"))}')

        def process_one(t):
            code, nid, pubdate, db_products = t
            key = f'{code}:{nid}'
            cap = captions.get((code, nid))
            if not cap:
                return {'code': code, 'key': key, 'error': '캡션 없음', 'updates': []}
            parsed, err = retry_llm(lambda: call_llm(
                GONGGU_CLASSIFY_SYSTEM, build_gonggu_classify_user(cap, pubdate, '')))
            if err or not parsed:
                return {'code': code, 'key': key, 'error': err or '분류 실패', 'updates': []}
            updates = match_periods(parsed.get('products') or [], db_products)
            return {'code': code, 'key': key, 'error': None, 'updates': updates,
                    'n_products': len(db_products)}

        def persist(r):
            if r['updates'] and not r['error']:
                dst.ping(reconnect=True)
                with dst.cursor() as cur:
                    for pid, start, end in r['updates']:
                        cur.execute(UPDATE_SQL[r['code']],
                                    (start, end, _compute_stage(start, end), pid))
                dst.commit()
            append_jsonl(CHECKPOINT_FILE, {'key': r['key'], 'n_updated': len(r['updates']),
                                           'error': r['error']})

        counters = run_llm_batch(todo, process_one, persist,
                                 concurrency=int(os.environ.get('CONCURRENCY', '4')),
                                 error_of=lambda r: r['error'])
        cp = load_jsonl(CHECKPOINT_FILE)
        updated = sum(rec.get('n_updated', 0) for rec in cp.values())
        print(f'완료 — 이번 배치 성공 {counters["ok"]} / 실패 {counters["err"]}, '
              f'누적 갱신 상품 {updated}개 (체크포인트 {len(cp)}개 포스트)')
    finally:
        dst.close()


if __name__ == '__main__':
    main()
