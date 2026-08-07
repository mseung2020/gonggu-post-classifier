"""플랫폼(ig/yt) 메타테이블 — post/video로 두 벌씩 복제되던 테이블명·컬럼명·SQL의 유일한
정의처(대공사 2단계 B4, 2026-08-05).

배경: DB 컬럼명은 원본 hifen DB의 대응 컬럼을 그대로 따르는 게 의도된 설계라(README/DDL 주석
참고) 인스타는 publish_date(스네이크), 유튜브는 publishDate(카멜)처럼 서로 다르다. 그 차이를
없애는 게 아니라 **한 곳에만 적어두고** 나머지 코드는 이 메타를 참조하게 한다 — 예전엔
load/rescan/backfill/update_stage마다 post용·video용 SQL 상수가 쌍으로 복제되어 있어서
"포스트 쪽만 고치고 비디오 쪽을 깜빡"하는 사고가 구조적으로 가능했다.

여기서 생성한 SQL이 리팩터링 전의 문자열과 동일한지는 tests/test_platforms.py가 못박는다.
테이블/컬럼명은 전부 이 파일의 코드 상수에서만 오므로 SQL 인젝션 여지는 없다.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Platform:
    code: str            # 'ig' | 'yt'
    parent_table: str    # gonggu_post | gonggu_video
    product_table: str   # gonggu_post_product | gonggu_video_product
    id_col: str          # post_id | video_id  (부모의 자연키이자 상품 테이블의 FK 컬럼명)
    date_col: str        # publish_date | publishDate  (원본 컬럼명 컨벤션 그대로)
    # 부모 INSERT에 들어가는 컬럼(원본 hifen 컬럼명 순서 유지). 유튜브의 external_url은
    # 채널 정보란 링크(resolve 단계에서 채워질 수 있음)로 인스타에는 없는 컬럼이다.
    parent_insert_cols: tuple
    # rescan/backfill이 "이 부모가 누구인지" 들고 다니는 식별/컨텍스트 컬럼(classification_note 제외).
    parent_ctx_cols: tuple


PLATFORMS = {
    'ig': Platform(
        code='ig',
        parent_table='gonggu_post',
        product_table='gonggu_post_product',
        id_col='post_id',
        date_col='publish_date',
        parent_insert_cols=('post_id', 'user_id', 'url', 'publish_date',
                            'is_calendar_feed', 'classification_note'),
        parent_ctx_cols=('post_id', 'user_id', 'url', 'publish_date'),
    ),
    'yt': Platform(
        code='yt',
        parent_table='gonggu_video',
        product_table='gonggu_video_product',
        id_col='video_id',
        date_col='publishDate',
        parent_insert_cols=('video_id', 'channel_id', 'title', 'video_url', 'external_url',
                            'publishDate', 'is_calendar_feed', 'classification_note'),
        parent_ctx_cols=('video_id', 'channel_id', 'video_url', 'publishDate'),
    ),
}

# 상품 테이블 컬럼은 두 플랫폼이 완전히 같다(FK 컬럼명만 id_col로 다름).
# 공구기간/스테이지는 포스트가 아니라 상품 단위로 이전됨(대공사 2026-08-06).
PRODUCT_INSERT_COLS = ('product_name', 'link_location', 'url_type', 'candidate_url',
                       'link_status', 'sort_order',
                       'gonggu_start_date', 'gonggu_end_date', 'gonggu_stage',
                       'link_note')


def native_id(code, parent):
    """부모 레코드(dict)에서 그 플랫폼의 자연키 값을 꺼낸다."""
    return parent.get(PLATFORMS[code].id_col)


def parent_insert_sql(p):
    cols = ', '.join(p.parent_insert_cols)
    vals = ', '.join(f'%({c})s' for c in p.parent_insert_cols)
    return f'INSERT INTO {p.parent_table} ({cols}) VALUES ({vals})'


def parent_exists_sql(p):
    return f'SELECT id FROM {p.parent_table} WHERE {p.id_col} = %s'


def product_insert_sql(p):
    cols = ', '.join((p.id_col,) + PRODUCT_INSERT_COLS)
    vals = ', '.join(f'%({c})s' for c in (p.id_col,) + PRODUCT_INSERT_COLS)
    return f'INSERT INTO {p.product_table} ({cols}) VALUES ({vals})'


def product_update_link_sql(p):
    """rescan이 쓰는 UPDATE — 재탐색으로 상태가 바뀌면 candidate_url/link_status와 함께
    link_note(왜 이 상태인지, LLM#3/결정적 사유)도 같이 갱신한다. updated_at을 NOW()로
    강제 갱신하는 이유는 rescan_inprogress.py 상단 주석 참고(값이 안 바뀌면 자동 트리거가 안 탐)."""
    return f'UPDATE {p.product_table} SET candidate_url = %s, link_status = %s, link_note = %s, updated_at = NOW() WHERE id = %s'


def product_update_period_sql(p):
    """backfill_period가 쓰는 UPDATE — 상품의 공구기간과 stage를 함께 갱신(상품 이전, 2026-08-06).
    기간/스테이지가 상품 단위로 옮겨졌으므로 product 테이블의 PK(id) 기준으로 UPDATE한다."""
    return (f'UPDATE {p.product_table} SET gonggu_start_date=%s, gonggu_end_date=%s, gonggu_stage=%s '
            f'WHERE id=%s')


def parent_ctx_from_row(p, row):
    """DB row에서 부모 컨텍스트 dict를 만든다(rescan/backfill 공용) — 날짜 컬럼은 기존 코드와
    동일하게 str()로 직렬화하고, classification_note를 함께 담는다."""
    ctx = {}
    for c in p.parent_ctx_cols:
        ctx[c] = str(row[c]) if c == p.date_col else row[c]
    ctx['classification_note'] = row['classification_note']
    return ctx
