#!/usr/bin/env python3
"""일회성 소급 스크립트 — 부모 테이블에 새로 추가된 "원본에서 가져오는 필드"를 hifen에서 다시
읽어 채운다. (2026-08-21)

대상 컬럼:
    gonggu_post.description    <- instagram_post_description.description
    gonggu_post.username       <- instagram_user.username            (post.user_id로 조인)
    gonggu_video.description   <- YT_video_lists_detail.video_description  또는 brand.video_description
    gonggu_video.channel_name  <- youtuber_info.title                (channel_id로 조인)
    gonggu_video.title         <- 위 두 원본 테이블의 title (NULL로 남은 행만)

description과 username/channel_name을 **한 스크립트로 합친 이유**: 대상이 완전히 같은 행이고
같은 SRC 연결을 쓰므로, 스크립트를 둘로 나누면 같은 행을 두 번 조회하고 폐기 시점도 두 번
관리해야 한다. (이 파일의 전신은 _backfill_description.py이며, 배포 전에 이 파일로 합쳤다.)

## 멱등성 / 체크포인트

대상 선정이 "채울 컬럼 중 하나라도 NULL"이라 **조건 자체가 체크포인트**다. 상태 파일이 없고,
몇 번을 돌려도 이미 채운 값은 UPDATE의 COALESCE가 보존한다. 중간에 죽으면 그냥 다시 돌리면 된다.

## 왜 파이프라인이 아니라 이 경로여야 하는가

- transform은 증분 모드라 "내용이 바뀐 02_classified 날짜 파일"만 다시 계산한다.
- classify의 dedup은 key(post_id/video_id) 단위라 이미 분류된 건은 재분류되지 않는다. 즉 옛 02
  레코드에는 caption_raw/username/channel_name 키가 영원히 생기지 않는다.
- load는 INSERT-only라 이미 있는 행을 UPDATE하지 않는다.
따라서 원본을 들고 있는 hifen에서 직접 다시 읽어 UPDATE하는 이 경로가 유일한 소급 수단이다.

## 유튜브 원본이 두 갈래인 점 주의

- fetch_source.py 경로 -> YT_video_lists_detail (키워드 매칭)
- fetch_yt_ppl.py 경로 -> brand (PPL)
gonggu_video만 보고는 어느 쪽으로 들어온 영상인지 알 수 없으므로 **detail을 먼저 보고, 없으면
brand로 보완**한다. 한쪽만 보면 다른 경로로 들어온 영상이 통째로 누락된다.
channel_name은 사정이 다르다 — youtuber_info는 channel_id가 PK고 현재 gonggu_video의 고유
channel_id 1,363개가 전부 매칭되므로(2026-08-21 실측), 영상이 hifen에서 삭제되어 두 원본
테이블 어디에도 없더라도 **우리가 이미 들고 있는 channel_id로 직접** 채울 수 있다. 그래서
channel_name은 별도 패스로 한 번 더 훑는다(_fill_channel_names).

## 사용법

    python3 -m gonggu.tools._backfill_parent_fields              # dry-run(기본): 대상 건수만
    python3 -m gonggu.tools._backfill_parent_fields --yes        # 실제 UPDATE
    LIMIT=500 python3 -m gonggu.tools._backfill_parent_fields --yes   # 이번 실행 500건만

## 수명 / 폐기 계획

이 파일은 영구 코드가 아니다. 예정된 경로:
 1. 배포 직후 1회 실행 -> 과거 행 전부 채움.
 2. 이후 1~2주간 매일 재실행(daily.py에 critical=False로 임시 등록해도 됨). 이유: classify
    재시도로 **옛 날짜의 02 파일에 레코드가 나중에 append**될 수 있고, 그 레코드의 부모에는
    새 키가 없어 NULL로 INSERT되기 때문이다.
 3. 그 다음 이 "NULL 톱업" 로직을 gonggu/pipeline/maintenance.py로 이관하고(이미 매일 마지막에
    도는 비필수 단계) 이 파일은 `git rm`으로 삭제한다. 이관해두면 2번의 구멍이 영구히 막히므로
    "지우고 잊는" 것보다 이 순서를 권장한다.
 4. queries/*.sql은 삭제하지 않는다 — queries/는 적용 완료 DDL의 기록 보관소다.
"""
import os
import sys

from gonggu.infra.common import connect_dst, connect_src

LIMIT = int(os.environ.get('LIMIT', '0'))
CHUNK = 500   # IN (...) 한 번에 물어볼 자연키 개수


def _pending(cur, table, cols, null_cols):
    """null_cols 중 하나라도 NULL인 행을 cols만 골라 가져온다. LIMIT은 환경변수로 조절."""
    where = ' OR '.join(f'{c} IS NULL' for c in null_cols)
    sql = f'SELECT {", ".join(cols)} FROM {table} WHERE {where} ORDER BY id'
    if LIMIT:
        sql += f' LIMIT {LIMIT}'
    cur.execute(sql)
    return cur.fetchall()


def _chunks(seq, n=CHUNK):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _ph(n):
    return ', '.join(['%s'] * n)


# ---------------------------------------------------------------- 인스타

IG_SRC_SQL = """
SELECT p.post_id AS post_id, d.description AS description, iu.username AS username
FROM instagram_post p
LEFT JOIN instagram_post_description d ON d.post_id = p.post_id
LEFT JOIN instagram_user iu ON iu.user_id = p.user_id
WHERE p.post_id IN ({ph})
"""


def backfill_ig(src_cur, dst_conn, dst_cur, apply):
    rows = _pending(dst_cur, 'gonggu_post', ('post_id',), ('description', 'username'))
    ids = [r['post_id'] for r in rows]
    if not ids:
        print('gonggu_post: 채울 행 없음')
        return 0, 0

    src = {}
    for part in _chunks(ids):
        src_cur.execute(IG_SRC_SQL.format(ph=_ph(len(part))), part)
        for r in src_cur.fetchall():
            src[r['post_id']] = (r['description'], r['username'])

    # COALESCE로 기존 값을 덮지 않는다 — 이미 채워진 컬럼은 그대로 두고 NULL만 메운다.
    params = [(d, u, pid) for pid, (d, u) in ((i, src[i]) for i in ids if i in src)]
    miss = len(ids) - len(params)
    print(f'gonggu_post: 대상 {len(ids)}건 / 원본 확보 {len(params)}건 / 원본 없음 {miss}건')
    if apply and params:
        dst_cur.executemany(
            'UPDATE gonggu_post SET description = COALESCE(description, %s), '
            'username = COALESCE(username, %s) WHERE post_id = %s', params)
        dst_conn.commit()
    return len(params), miss


# ---------------------------------------------------------------- 유튜브

YT_DETAIL_SQL = """
SELECT d.video_id AS video_id, d.video_description AS description, d.title AS title,
       yi.title AS channel_name
FROM YT_video_lists_detail d
LEFT JOIN YT_video_lists v ON v.video_id = d.video_id
LEFT JOIN youtuber_info yi ON yi.channel_id = v.channel_id
WHERE d.video_id IN ({ph})
"""

YT_BRAND_SQL = """
SELECT b.video_id AS video_id, b.video_description AS description, b.title AS title,
       yi.title AS channel_name
FROM brand b
LEFT JOIN youtuber_info yi ON yi.channel_id = b.channel_id
WHERE b.video_id IN ({ph})
"""


def backfill_yt(src_cur, dst_conn, dst_cur, apply):
    rows = _pending(dst_cur, 'gonggu_video', ('video_id', 'channel_id'),
                    ('description', 'channel_name', 'title'))
    if not rows:
        print('gonggu_video: 채울 행 없음')
        return 0, 0
    ids = [r['video_id'] for r in rows]

    src = {}
    for sql in (YT_DETAIL_SQL, YT_BRAND_SQL):
        todo = [v for v in ids if v not in src]   # detail 우선, brand는 못 찾은 것만 보완
        for part in _chunks(todo):
            src_cur.execute(sql.format(ph=_ph(len(part))), part)
            for r in src_cur.fetchall():
                src[r['video_id']] = (r['description'], r['title'], r['channel_name'])

    params = [(src[v][0], src[v][1], src[v][2], v) for v in ids if v in src]
    miss = len(ids) - len(params)
    print(f'gonggu_video: 대상 {len(ids)}건 / 원본 확보 {len(params)}건 / 원본 없음 {miss}건')
    if apply and params:
        dst_cur.executemany(
            'UPDATE gonggu_video SET description = COALESCE(description, %s), '
            'title = COALESCE(title, %s), channel_name = COALESCE(channel_name, %s) '
            'WHERE video_id = %s', params)
        dst_conn.commit()

    # 영상이 hifen에서 삭제돼 위 두 테이블에 없어도, 우리가 든 channel_id로 채널명은 채울 수 있다.
    extra = _fill_channel_names(src_cur, dst_conn, dst_cur, rows, apply)
    return len(params) + extra, miss


def _fill_channel_names(src_cur, dst_conn, dst_cur, rows, apply):
    """channel_name만 남은 행을 channel_id -> youtuber_info로 직접 채운다(위 docstring 참고)."""
    todo = {r['channel_id'] for r in rows if r.get('channel_id')}
    if not todo:
        return 0
    names = {}
    for part in _chunks(sorted(todo)):
        src_cur.execute(
            f'SELECT channel_id, title FROM youtuber_info WHERE channel_id IN ({_ph(len(part))})',
            part)
        for r in src_cur.fetchall():
            names[r['channel_id']] = r['title']
    params = [(names[r['channel_id']], r['video_id'])
              for r in rows if r.get('channel_id') in names]
    if not params:
        return 0
    print(f'gonggu_video: channel_id 직접 조회로 채널명 보완 가능 {len(params)}건 '
          f'(고유 채널 {len(names)}/{len(todo)} 매칭)')
    if apply:
        dst_cur.executemany(
            'UPDATE gonggu_video SET channel_name = COALESCE(channel_name, %s) '
            'WHERE video_id = %s', params)
        dst_conn.commit()
    return 0   # 위 backfill_yt 집계와 겹치므로 합계에는 더하지 않는다


def main():
    apply = '--yes' in sys.argv
    if not apply:
        print('[dry-run] 실제로 쓰려면 --yes 를 붙일 것\n')

    src = connect_src()
    dst = connect_dst()
    try:
        with src.cursor() as src_cur, dst.cursor() as dst_cur:
            ig_hit, ig_miss = backfill_ig(src_cur, dst, dst_cur, apply)
            yt_hit, yt_miss = backfill_yt(src_cur, dst, dst_cur, apply)
    finally:
        src.close()
        dst.close()

    verb = '갱신' if apply else '갱신 예정'
    print(f'\n합계 {verb} {ig_hit + yt_hit}건, 원본을 못 찾은 건 {ig_miss + yt_miss}건')
    if ig_miss + yt_miss:
        print('원본 없음 = hifen에서 그 사이 삭제된 게시물일 가능성이 크다. 다음 실행에도 계속 '
              '대상으로 잡히므로, 건수가 안 줄고 고정되면 정상이다.')


if __name__ == '__main__':
    main()
