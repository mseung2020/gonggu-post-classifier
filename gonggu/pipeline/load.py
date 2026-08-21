#!/usr/bin/env python3
"""4단계: load_ready.json(또는 resolve_links를 거친 load_ready_resolved.json)을
dev_gongguking의 gonggu_video/gonggu_video_product(유튜브) 또는
gonggu_post/gonggu_post_product(인스타그램)에 INSERT한다.
이미 있는 (post_id) / (video_id)는 건너뛴다(덮어쓰지 않음 — 다운스트림에서 이미 손댔을 수 있음).

사용법:
    python3 scripts/load.py
"""
import os

import pymysql

from gonggu.common import LOAD_READY_DIR, RESOLVED_DIR, connect_dst, load_json_dir
from gonggu.platforms import (PLATFORMS, native_id, parent_exists_sql, parent_insert_sql,
                              parent_keys_sql, product_insert_sql)


def _item_key(item):
    return f"{item['platform']}:{native_id(item['platform'], item.get('parent') or {})}"


def split_unresolved(resolved, ready):
    """(04에 있는 items, 03에만 있는 items)로 나눈다.

    04_resolved는 resolve_links가 돌 때마다 그 시점의 전체로 재조립되므로, transform은
    돌았는데 resolve가 안 돈 경우(--skip-resolve, 또는 resolve가 중간에 죽은 날) 04에는
    새 포스트가 없다 — 예전엔 이때 04만 읽어서 그 새 포스트들이 에러도 없이 적재 대상에서
    조용히 빠졌다(2026-08-05 감사 A3에서 발견)."""
    resolved_keys = {_item_key(i) for i in resolved}
    return resolved, [i for i in ready if _item_key(i) not in resolved_keys]


def load_items():
    """resolve_links를 돌렸으면 candidate_url이 "찐 최종 링크 하나"로 좁혀진 04_resolved를
    기본으로 쓰고, 아직 안 돌렸으면(또는 스킵했으면) transform.py 원본(03_load_ready)을 쓴다.

    03에만 있는(아직 해석 전) 항목은 **기본적으로 이번 적재에서 보류하고 경고만 출력**한다 —
    load는 UPDATE를 안 하므로 원본 후보(세미콜론 목록, link_status=NULL)로 한번 들어가면
    나중에 resolve 결과가 그 행에 영영 반영되지 못하고, link_status가 NULL이라
    rescan_inprogress의 재탐색 대상에도 안 잡히기 때문이다. 다음 resolve 실행 후 load를
    다시 돌리면 자동으로 포함된다(예전과 같은 복구 경로 — 달라진 건 "조용히"가 아니라
    경고가 보인다는 것). 해석 없이 원본 후보로라도 지금 적재하고 싶으면
    LOAD_UNRESOLVED=1로 명시적으로 켠다."""
    resolved = load_json_dir(RESOLVED_DIR)
    ready = load_json_dir(LOAD_READY_DIR)
    if not resolved:
        print(f'입력 폴더: {LOAD_READY_DIR} (04_resolved 없음 — 원본 후보 그대로 적재)')
        return ready
    items, unresolved = split_unresolved(resolved, ready)
    print(f'입력 폴더: {RESOLVED_DIR} ({len(items)}건)')
    if unresolved:
        if os.environ.get('LOAD_UNRESOLVED') == '1':
            print(f'  ⚠ 03_load_ready에만 있는(링크 해석 전) {len(unresolved)}건을 LOAD_UNRESOLVED=1 '
                  f'지정에 따라 원본 후보(세미콜론 목록) 그대로 적재합니다 — 이 행들은 이후 resolve '
                  f'결과가 반영되지 않습니다(load는 UPDATE 없음).')
            items = items + unresolved
        else:
            print(f'  ⚠ 03_load_ready에만 있는(링크 해석 전) {len(unresolved)}건은 이번 적재에서 '
                  f'보류합니다 — resolve_links를 돌린 뒤 load를 다시 실행하면 포함됩니다. '
                  f'(해석 없이 지금 넣으려면 LOAD_UNRESOLVED=1)')
    return items

# SQL은 전부 platforms.py 메타테이블에서 생성한다(2단계 B4) — post/video용 상수가 두 벌씩
# 복제되어 있던 구조를 접었다. 생성된 문자열이 리팩터링 전과 동일함은 tests/test_platforms.py가 보증.


def existing_keys(cur):
    """이미 적재된 부모 자연키 전체를 {(code, key)} 집합으로 한 번에 읽는다(4단계, 2026-08-20).

    왜: 예전엔 load_item이 항목마다 `SELECT ... WHERE post_id=%s`를 던졌다. 그런데 이 단계는
    매일 04_resolved **전체**(누적)를 훑으면서 그날 새로 생긴 것만 넣는 구조라, 대부분이
    "이미 있음" 확인에만 쓰이는 왕복이 된다 — 실측(2026-08-20): 45,566건 중 43,737건(96%)이
    스킵이었고, 존재확인 1건이 6.06ms라 그 왕복만 약 276초였다. 같은 키를 한 번에 가져오면
    0.16초다. 소배치 커밋(4단계 D2)이 커밋 왕복을 1/50로 줄였는데 이 확인 왕복은 건당 1회로
    남아 있어서, 이 단계 시간의 거의 전부를 차지하고 있었다.

    ⚠ 경합 안전성: 다른 프로세스가 그 사이에 같은 키를 넣으면 이 집합은 낡은 정보가 된다.
    그래도 안전한 이유는 DB의 UNIQUE(post_id/video_id)가 최종 방어선이고, 그 충돌(errno 1062)을
    _is_duplicate_entry가 이미 "실패가 아니라 스킵"으로 처리하기 때문이다(2026-08-05 감사 A4).
    즉 이 집합은 왕복을 줄이는 캐시일 뿐 정확성의 근거가 아니다."""
    keys = set()
    for code, p in PLATFORMS.items():
        cur.execute(parent_keys_sql(p))
        keys |= {(code, r[p.id_col]) for r in cur.fetchall()}
    return keys


def load_item(cur, code, parent, products, known=None):
    """부모 1건 + 상품 N건 INSERT. 이미 있으면(자연키 기준) 아무것도 안 하고 False.

    known이 주어지면 그 집합으로 존재를 판단하고(DB 왕복 없음), 넣은 키는 집합에 추가한다 —
    같은 실행 안에 같은 키가 두 번 나오는 입력에서도 두 번 INSERT하지 않기 위함이다.
    known=None이면 예전처럼 건건이 DB에 묻는다(테스트/단건 경로 호환)."""
    p = PLATFORMS[code]
    key = parent[p.id_col]
    if known is not None:
        if (code, key) in known:
            return False
    else:
        cur.execute(parent_exists_sql(p), (key,))
        if cur.fetchone():
            return False
    # 유튜브: 캡션에 링크가 있던 영상은 채널 정보란까지 긁어볼 필요가 없어서 external_url이
    # 없을 수 있어 기본값 None을 깔아준다(인스타 INSERT에는 이 컬럼이 없어 그냥 무시됨).
    # is_calendar_feed도 스키마 개편 전(2026-08-06)에 만들어진 옛 03 파일엔 없을 수 있어 기본 0.
    # description(원문 캡션)/username/channel_name도 도입(2026-08-21) 전 03 파일엔 없어 기본
    # None — 그렇게 NULL로 들어간 건은 gonggu/tools/_backfill_parent_fields.py가 hifen에서
    # 소급해 채운다. (인스타 INSERT에 없는 channel_name, 유튜브에 없는 username은 각 플랫폼의
    #  parent_insert_cols에 안 들어 있으므로 그냥 무시된다 — external_url과 같은 방식.)
    cur.execute(parent_insert_sql(p),
                {'external_url': None, 'is_calendar_feed': 0, 'description': None,
                 'username': None, 'channel_name': None, **parent})
    for prod in products:
        # resolve_links를 안 거친 03_load_ready로 돌아가는 경우 link_status 키가 없을 수 있고,
        # 스키마 개편 전 옛 03 파일엔 상품별 기간 필드가 없을 수 있어 기본값 None을 깔아준다.
        # FK 컬럼명은 부모의 자연키 이름 그대로(id_col).
        cur.execute(product_insert_sql(p),
                    {'link_status': None, 'gonggu_start_date': None, 'gonggu_end_date': None,
                     'gonggu_stage': None, 'link_note': None, **prod, p.id_col: key})
    if known is not None:
        known.add((code, key))
    return True


def _is_duplicate_entry(e):
    """UNIQUE(post_id/video_id) 충돌 — 두 프로세스가 "존재확인 → INSERT" 사이에 서로
    끼어들었을 때만 나는 에러(README에도 기록된 경합). 데이터가 깨진 게 아니라 "다른
    쪽이 먼저 넣었다"는 뜻이므로 실패가 아니라 스킵으로 센다(2026-08-05 감사 A4).
    INSERT IGNORE로 바꾸지 않는 이유: IGNORE는 중복뿐 아니라 컬럼 길이 초과 같은 다른
    에러까지 경고로 삼켜 잘린 값이 조용히 들어가므로, 중복(errno 1062)만 정확히 잡는다."""
    return isinstance(e, pymysql.err.IntegrityError) and e.args and e.args[0] == 1062


def _load_one_committed(conn, cur, item, known=None):
    """한 건 처리 + 즉시 커밋(예전 방식). 반환: 'inserted' | 'skipped' | 'failed'.

    ⚠ known은 일부러 안 넘긴다(기본 None) — 여기로 오는 건 "배치가 실패해서 롤백된 뒤" 다시
    보는 경로다. 롤백으로 DB 상태가 되돌아갔는데 메모리 집합엔 "넣었다"가 남아 있으면 그
    건을 영영 스킵하게 된다. 그래서 이 경로만은 DB에 직접 물어 진실을 다시 확인한다."""
    key = native_id(item['platform'], item['parent'])
    try:
        ok = load_item(cur, item['platform'], item['parent'], item['products'], known)
        conn.commit()
    except Exception as e:
        conn.rollback()
        if _is_duplicate_entry(e):
            return 'skipped'
        print(f'  실패: {item["platform"]} {key} — {e}')
        return 'failed'
    return 'inserted' if ok else 'skipped'


def load_all(conn, items, batch_size):
    """소배치 커밋(4단계 D2, 2026-08-05) — 예전엔 건당 커밋이라 항목마다 DB 왕복이
    3~4회였다. 이제 batch_size(기본 50)건을 한 트랜잭션으로 처리하고, 배치 안에서 뭐 하나라도
    실패하면 그 배치만 롤백한 뒤 예전 방식(건별 커밋)으로 재처리한다 — "한 건의 실패가 다른
    건의 적재를 막지 않는다"는 기존 보장이 그대로 유지되면서(실패 배치만 건별로 격리),
    정상 경로의 커밋 왕복이 1/batch_size로 줄어든다. LOAD_BATCH=1이면 사실상 예전과 동일.
    반환: (inserted, skipped, failed)."""
    counts = {'inserted': 0, 'skipped': 0, 'failed': 0}
    with conn.cursor() as cur:
        # 존재확인용 자연키를 한 번에 읽어둔다(existing_keys 주석 — 예전엔 건당 왕복이라
        # 45,566건에 약 276초였다). 실패 배치의 건별 재처리는 이 집합을 안 쓰고 DB에 직접 묻는다.
        known = existing_keys(cur)
        for i in range(0, len(items), batch_size):
            batch = items[i:i + batch_size]
            snapshot = set(known)   # 롤백되면 메모리 집합도 이 시점으로 되돌려야 진실과 안 어긋난다
            try:
                results = [load_item(cur, it['platform'], it['parent'], it['products'], known)
                           for it in batch]
                conn.commit()
                counts['inserted'] += sum(1 for ok in results if ok)
                counts['skipped'] += sum(1 for ok in results if not ok)
            except Exception:
                conn.rollback()  # 이 배치에서 이미 실행된 INSERT까지 전부 되돌리고 건별로 재시도
                known.clear()
                known.update(snapshot)
                for it in batch:
                    counts[_load_one_committed(conn, cur, it)] += 1
                # 건별 재처리로 실제 들어간 것들을 집합에 반영(다음 배치가 또 넣으려 하지 않게).
                for it in batch:
                    p = PLATFORMS[it['platform']]
                    cur.execute(parent_exists_sql(p), (it['parent'][p.id_col],))
                    if cur.fetchone():
                        known.add((it['platform'], it['parent'][p.id_col]))
    return counts['inserted'], counts['skipped'], counts['failed']


def main():
    items = load_items()
    batch_size = max(1, int(os.environ.get('LOAD_BATCH', '50')))
    conn = connect_dst()
    try:
        inserted, skipped, failed = load_all(conn, items, batch_size)
    finally:
        conn.close()
    print(f'삽입 {inserted}건 / 이미 존재해서 스킵 {skipped}건 / 실패 {failed}건 (전체 {len(items)}건)')


if __name__ == '__main__':
    main()
