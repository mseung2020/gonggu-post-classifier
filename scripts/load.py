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

from common import LOAD_READY_DIR, RESOLVED_DIR, connect_dst, load_json_dir


def _item_key(item):
    parent = item.get('parent') or {}
    native_id = parent.get('post_id') if item['platform'] == 'ig' else parent.get('video_id')
    return f"{item['platform']}:{native_id}"


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

INSERT_VIDEO = """
INSERT INTO gonggu_video
    (video_id, channel_id, title, video_url, external_url, publishDate, gonggu_start_date,
     gonggu_end_date, gonggu_stage, classification_note)
VALUES (%(video_id)s, %(channel_id)s, %(title)s, %(video_url)s, %(external_url)s, %(publishDate)s,
        %(gonggu_start_date)s, %(gonggu_end_date)s, %(gonggu_stage)s, %(classification_note)s)
"""
CHECK_VIDEO_EXISTS = "SELECT id FROM gonggu_video WHERE video_id = %s"
INSERT_VIDEO_PRODUCT = """
INSERT INTO gonggu_video_product
    (video_id, product_name, link_location, url_type, candidate_url, link_status, sort_order)
VALUES (%(video_id)s, %(product_name)s, %(link_location)s, %(url_type)s, %(candidate_url)s,
        %(link_status)s, %(sort_order)s)
"""

INSERT_POST = """
INSERT INTO gonggu_post
    (post_id, user_id, url, publish_date, gonggu_start_date, gonggu_end_date, gonggu_stage,
     classification_note)
VALUES (%(post_id)s, %(user_id)s, %(url)s, %(publish_date)s,
        %(gonggu_start_date)s, %(gonggu_end_date)s, %(gonggu_stage)s, %(classification_note)s)
"""
CHECK_POST_EXISTS = "SELECT id FROM gonggu_post WHERE post_id = %s"
INSERT_POST_PRODUCT = """
INSERT INTO gonggu_post_product
    (post_id, product_name, link_location, url_type, candidate_url, link_status, sort_order)
VALUES (%(post_id)s, %(product_name)s, %(link_location)s, %(url_type)s, %(candidate_url)s,
        %(link_status)s, %(sort_order)s)
"""


def load_video(cur, parent, products):
    cur.execute(CHECK_VIDEO_EXISTS, (parent['video_id'],))
    if cur.fetchone():
        return False
    # 캡션에 링크가 있던 영상은 채널 정보란까지 긁어볼 필요가 없어서 external_url이 없을 수
    # 있어 기본값 None을 깔아준다.
    cur.execute(INSERT_VIDEO, {'external_url': None, **parent})
    video_id = parent['video_id']  # FK 컬럼명이 gonggu_video_product.video_id로 되어있음(자연키)
    for p in products:
        # resolve_links를 안 거친 load_ready.json으로 돌아가는 경우 link_status 키가 없을
        # 수 있어 기본값 None을 깔아준다.
        cur.execute(INSERT_VIDEO_PRODUCT, {'link_status': None, **p, 'video_id': video_id})
    return True


def load_post(cur, parent, products):
    cur.execute(CHECK_POST_EXISTS, (parent['post_id'],))
    if cur.fetchone():
        return False
    cur.execute(INSERT_POST, parent)
    post_id = parent['post_id']  # FK 컬럼명이 gonggu_post_product.post_id로 되어있음(자연키)
    for p in products:
        cur.execute(INSERT_POST_PRODUCT, {'link_status': None, **p, 'post_id': post_id})
    return True


def _is_duplicate_entry(e):
    """UNIQUE(post_id/video_id) 충돌 — 두 프로세스가 "존재확인 → INSERT" 사이에 서로
    끼어들었을 때만 나는 에러(README에도 기록된 경합). 데이터가 깨진 게 아니라 "다른
    쪽이 먼저 넣었다"는 뜻이므로 실패가 아니라 스킵으로 센다(2026-08-05 감사 A4).
    INSERT IGNORE로 바꾸지 않는 이유: IGNORE는 중복뿐 아니라 컬럼 길이 초과 같은 다른
    에러까지 경고로 삼켜 잘린 값이 조용히 들어가므로, 중복(errno 1062)만 정확히 잡는다."""
    return isinstance(e, pymysql.err.IntegrityError) and e.args and e.args[0] == 1062


def main():
    items = load_items()
    conn = connect_dst()
    inserted, skipped, failed = 0, 0, 0
    try:
        with conn.cursor() as cur:
            for item in items:
                fn = load_video if item['platform'] == 'yt' else load_post
                key = item['parent'].get('video_id') or item['parent'].get('post_id')
                # 한 건씩 커밋 — 한 건의 INSERT 실패(예: LLM이 준 값이 컬럼 길이/제약을 벗어남)가
                # 이미 이번 실행에서 성공적으로 넣은 다른 건들까지 롤백시키지 않도록 함.
                try:
                    ok = fn(cur, item['parent'], item['products'])
                    conn.commit()
                except Exception as e:
                    conn.rollback()
                    if _is_duplicate_entry(e):
                        skipped += 1
                        continue
                    failed += 1
                    print(f'  실패: {item["platform"]} {key} — {e}')
                    continue
                if ok:
                    inserted += 1
                else:
                    skipped += 1
    finally:
        conn.close()
    print(f'삽입 {inserted}건 / 이미 존재해서 스킵 {skipped}건 / 실패 {failed}건 (전체 {len(items)}건)')


if __name__ == '__main__':
    main()
