#!/usr/bin/env python3
"""일회성: link_resolution.jsonl에 남아있는 상품별 판단 이유(note)를 DB 상품 행 link_note로 소급.

link_note 컬럼(2026-08-07 추가) 이전에 적재된 상품 행은 link_note가 전부 NULL이다. 링크 판단
이유 자체는 resolve_links가 예전부터 data/output/link_resolution.jsonl에 상품 key로 남겨왔으므로
(key = 'platform:native_id:sort_order', 예: 'ig:POST123:0'), 그 파일을 읽어 key로 DB 상품 행을
찾아 link_note만 UPDATE한다. 파일에만 있던 이유를 건바이건으로 DB에 올리는 것뿐 — 링크 자체
(candidate_url/link_status)나 기간은 손도 안 댄다.

⚠ 이미 link_note가 채워진 행은 건드리지 않는다(WHERE link_note IS NULL) — 이 스크립트 이후
rescan/재적재로 새로 채워진 최신 판단을 파일의 옛 기록이 덮어쓰지 못하게. 그래서 재실행해도
안전하다(idempotent). 커버리지는 jsonl에 key가 남아있는 만큼이고(중간에 파일을 비웠다면 그만큼
구멍), 나머지는 다음 rescan/재적재 때 자연히 채워진다.

sort_order는 한 게시물 안에서 상품 행을 고유하게 가리키므로 (native_id, sort_order)로 정확히
1행이 매칭된다. 매칭 안 되는(이미 지워졌거나 sort_order가 안 맞는) key는 그냥 0행 갱신으로 스킵.

사용법(저장소 루트에서):
    python3 -m gonggu._migrate_link_note
    LIMIT=100 python3 -m gonggu._migrate_link_note   # 소규모 테스트
"""
import os

from gonggu.common import connect_dst, load_jsonl
from gonggu.platforms import PLATFORMS
from gonggu.resolve_links.config import RESOLUTION_FILE

# link_note가 아직 비어있는 행만 채운다(최신 판단 보존). 매칭키는 자연키+sort_order.
UPDATE_SQL = {code: (f'UPDATE {p.product_table} SET link_note = %s '
                     f'WHERE {p.id_col} = %s AND sort_order = %s AND link_note IS NULL')
              for code, p in PLATFORMS.items()}


def parse_key(key):
    """'ig:POST123:0' → ('ig', 'POST123', 0). platform은 앞에서, sort_order는 뒤에서 떼고
    가운데는 통째로 native_id로 둔다(native_id에 ':'가 들어갈 일은 없지만 방어적)."""
    parts = str(key).split(':')
    if len(parts) < 3:
        return None
    code, sort_order, native_id = parts[0], parts[-1], ':'.join(parts[1:-1])
    if code not in PLATFORMS:
        return None
    try:
        return code, native_id, int(sort_order)
    except ValueError:
        return None


def main():
    records = load_jsonl(RESOLUTION_FILE)  # {key: {status, note, ...}} — 같은 key 마지막이 이김
    if not records:
        print(f'{RESOLUTION_FILE} 비어있음 — 소급할 것 없음.')
        return

    items = []
    skipped_no_note = skipped_bad_key = 0
    for key, rec in records.items():
        note = (rec.get('note') or '').strip()
        if not note:
            skipped_no_note += 1
            continue
        parsed = parse_key(key)
        if not parsed:
            skipped_bad_key += 1
            continue
        code, native_id, sort_order = parsed
        items.append((code, native_id, sort_order, note[:255]))

    limit = int(os.environ.get('LIMIT', '0')) or len(items)
    items = items[:limit]
    print(f'link_resolution.jsonl {len(records)}건 → note 있는 대상 {len(items)}건 소급 시도 '
          f'(note 없음 {skipped_no_note} / key 파싱불가 {skipped_bad_key} 제외)')

    dst = connect_dst()
    try:
        updated = 0
        with dst.cursor() as cur:
            for i, (code, native_id, sort_order, note) in enumerate(items, 1):
                cur.execute(UPDATE_SQL[code], (note, native_id, sort_order))
                updated += cur.rowcount
                if i % 500 == 0:
                    dst.commit()
                    print(f'  {i}/{len(items)} 처리 — 누적 갱신 {updated}행')
        dst.commit()
        print(f'완료 — 시도 {len(items)}건, 실제 갱신 {updated}행 '
              f'(이미 채워졌거나 매칭 안 된 나머지는 건너뜀)')
    finally:
        dst.close()


if __name__ == '__main__':
    main()
