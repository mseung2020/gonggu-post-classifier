"""대상 선정 + 원본 캡션 로딩.

대상: link_status='done'인 상품 중 detail 행이 아직 없거나 detail_status가 pending/error인 것.
DB 상태 자체가 체크포인트라서 별도 파일이 없다 — 첫 실행은 백로그 전수(백필), 이후 실행은
그날 새로 done이 된 것 + 지난 실행에서 error였던 것만 자동으로 잡힌다(idempotent).
gone(페이지 영구 소멸)과 done은 재시도하지 않는다.

detail/image 테이블·FK 컬럼명은 platforms.py 메타에서 규칙적으로 파생된다(DDL이 그렇게
설계됨): gonggu_post_product → gonggu_post_product_detail / gonggu_post_product_image /
post_product_id. platforms.py 자체는 수정하지 않는다(전반부 골든 diff 무풍).
"""
from gonggu.common import connect_src
from gonggu.platforms import PLATFORMS


def detail_table(p):
    return f'{p.product_table}_detail'


def image_table(p):
    return f'{p.product_table}_image'


def fk_col(p):
    """detail/image 테이블이 상품 행을 가리키는 FK 컬럼명 — post_product_id / video_product_id."""
    return p.product_table.replace('gonggu_', '') + '_id'


def select_targets_sql(p):
    """이번 실행 대상 상품 SELECT. 유튜브는 부모에 title 컬럼이 있어 LLM#4 입력으로 같이
    가져온다(인스타는 그 컬럼이 없어 빈 문자열로 통일)."""
    title_col = 'p.title AS parent_title,' if p.code == 'yt' else "'' AS parent_title,"
    return f"""
SELECT pp.id AS product_row_id, pp.product_name, pp.candidate_url,
       p.{p.id_col} AS native_id, p.gonggu_stage, p.classification_note,
       {title_col} p.{p.date_col} AS publish_date,
       d.detail_status AS prev_status
FROM {p.product_table} pp
JOIN {p.parent_table} p ON p.{p.id_col} = pp.{p.id_col}
LEFT JOIN {detail_table(p)} d ON d.{fk_col(p)} = pp.id
WHERE pp.link_status = 'done'
  AND (d.id IS NULL OR d.detail_status IN ('pending', 'error'))
"""


def fetch_targets(conn, only_platform=None):
    """(platform_code, row dict) 목록. row에는 select_targets_sql의 컬럼이 그대로 들어있다."""
    out = []
    with conn.cursor() as cur:
        for code, p in PLATFORMS.items():
            if only_platform and code != only_platform:
                continue
            cur.execute(select_targets_sql(p))
            for r in cur.fetchall():
                r['publish_date'] = str(r['publish_date'])
                out.append((code, r))
    return out


# 원본 캡션은 dev_gongguking에 없고 hifen(SRC)에만 있다 — fetch_source.py가 쓰는 것과 같은
# 테이블/컬럼에서 가져온다(인스타: instagram_post_description.description, 유튜브:
# YT_video_lists_detail.video_description). 상품마다 따로 조회하면 대상 수만큼 왕복이 생기니
# 실행 시작 때 IN 배치로 한 번에 가져와 dict로 들고 다닌다.
_CAPTION_SQL = {
    'ig': 'SELECT post_id AS k, description AS caption FROM instagram_post_description WHERE post_id IN ({ph})',
    'yt': ('SELECT video_id AS k, video_description AS caption, title '
           'FROM YT_video_lists_detail WHERE video_id IN ({ph})'),
}
_CHUNK = 500  # IN 절이 무한정 길어지지 않게


def fetch_captions(targets):
    """targets의 native_id들로 SRC DB에서 캡션을 배치 조회 → {(code, native_id): caption}.
    유튜브는 fetch_source.py와 동일하게 '[제목] ...' 프리픽스를 붙여 캡션 취급한다.
    SRC 조회가 통째로 실패하면 빈 dict를 돌려주고 호출부가 캡션 없이 진행한다(캡션은
    보조 입력이지 필수 입력이 아님 — 크롤링 결과만으로도 대부분의 필드는 채워진다)."""
    ids = {'ig': set(), 'yt': set()}
    for code, r in targets:
        ids[code].add(r['native_id'])
    captions = {}
    if not (ids['ig'] or ids['yt']):
        return captions
    try:
        conn = connect_src()
    except Exception as e:
        print(f'  ⚠ SRC(hifen) DB 연결 실패 — 캡션 없이 진행합니다: {str(e)[:120]}')
        return captions
    try:
        with conn.cursor() as cur:
            for code in ('ig', 'yt'):
                id_list = sorted(ids[code])
                for i in range(0, len(id_list), _CHUNK):
                    chunk = id_list[i:i + _CHUNK]
                    ph = ', '.join(['%s'] * len(chunk))
                    cur.execute(_CAPTION_SQL[code].format(ph=ph), chunk)
                    for r in cur.fetchall():
                        cap = r['caption'] or ''
                        if code == 'yt':
                            cap = f"[제목] {r.get('title') or ''}\n\n{cap}"
                        captions[(code, r['k'])] = cap
    finally:
        conn.close()
    return captions
