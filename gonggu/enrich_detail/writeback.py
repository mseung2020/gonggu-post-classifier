"""DB 반영 — detail UPSERT + image 전체 교체(상품 단위 트랜잭션).

전반부 load.py의 INSERT-only와 달리 이 테이블들은 재크롤링 시 UPDATE가 규약이다(DDL 주석:
"재크롤링해도 새 행을 만들지 않고 이 행을 UPDATE"). UNIQUE(post_product_id/video_product_id)
위에 INSERT ... ON DUPLICATE KEY UPDATE로 구현한다.

보수적 안전판: error/gone 결과는 상태 컬럼(detail_status/detail_error)만 갱신하는 별도
UPSERT를 쓴다 — 예전에 done으로 채워진 행이 재크롤링 실패로 데이터 필드까지 NULL로
덮이는 사고를 구조적으로 막는다(판정/데이터 완전 보존 원칙).

이미지는 DELETE 후 INSERT(전체 교체) — 갤러리는 "지금 페이지의 순서 있는 스냅샷"이라
부분 병합하면 옛 이미지와 새 이미지가 섞여 sort_order가 의미를 잃는다. detail과 같은
트랜잭션이라 중간에 죽어도 반쪽 상태로 남지 않는다.
"""
from gonggu.platforms import PLATFORMS

from .config import MAX_ERROR_LEN
from .targets import detail_table, fk_col, image_table

# detail 테이블의 데이터 필드(상태/키 제외) — validate.merge_and_validate 반환 키와 동일.
DATA_COLS = ('thumbnail_url', 'brand_name_kr', 'brand_name_en', 'category', 'subcategory',
             'search_keywords', 'original_price', 'sale_price', 'discount_rate',
             'discount_amount', 'free_shipping', 'shipping_fee', 'shipping_note',
             'composition_info', 'gift_info', 'coupon_info', 'ai_summary',
             'ai_summary_confidence')


def upsert_done_sql(p):
    cols = (fk_col(p),) + DATA_COLS + ('detail_status', 'detail_error')
    placeholders = ', '.join(f'%({c})s' for c in cols)
    updates = ', '.join(f'{c} = VALUES({c})' for c in DATA_COLS)
    return (f'INSERT INTO {detail_table(p)} ({", ".join(cols)}) VALUES ({placeholders}) '
            f'ON DUPLICATE KEY UPDATE {updates}, detail_status = VALUES(detail_status), '
            f'detail_error = NULL, updated_at = NOW()')


def upsert_status_sql(p):
    """error/gone 전용 — 데이터 필드는 건드리지 않고 상태만 기록/갱신."""
    return (f'INSERT INTO {detail_table(p)} ({fk_col(p)}, detail_status, detail_error) '
            f'VALUES (%(fk)s, %(status)s, %(error)s) '
            f'ON DUPLICATE KEY UPDATE detail_status = VALUES(detail_status), '
            f'detail_error = VALUES(detail_error), updated_at = NOW()')


def replace_images_sql(p):
    delete = f'DELETE FROM {image_table(p)} WHERE {fk_col(p)} = %s'
    insert = (f'INSERT INTO {image_table(p)} ({fk_col(p)}, image_url, image_type, sort_order) '
              f'VALUES (%s, %s, %s, %s)')
    return delete, insert


_DONE_SQL = {code: upsert_done_sql(p) for code, p in PLATFORMS.items()}
_STATUS_SQL = {code: upsert_status_sql(p) for code, p in PLATFORMS.items()}
_IMAGE_SQL = {code: replace_images_sql(p) for code, p in PLATFORMS.items()}


def write_done(db, code, product_row_id, fields, image_rows):
    """성공 결과 반영 — detail UPSERT + 이미지 전체 교체를 한 트랜잭션으로.
    thumbnail_url(detail 컬럼)은 image 테이블의 thumbnail 첫 행과 동일해야 한다는 DDL
    정합성 규약을 여기서 코드로 보장한다."""
    p = PLATFORMS[code]
    thumb = next((u for u, t, _ in image_rows if t == 'thumbnail'), None)
    params = {fk_col(p): product_row_id, 'detail_status': 'done', 'detail_error': None,
              'thumbnail_url': thumb, **fields}
    delete_sql, insert_sql = _IMAGE_SQL[code]
    db.ping(reconnect=True)
    try:
        with db.cursor() as cur:
            cur.execute(_DONE_SQL[code], params)
            cur.execute(delete_sql, (product_row_id,))
            if image_rows:
                cur.executemany(insert_sql,
                                [(product_row_id, u, t, o) for u, t, o in image_rows])
        db.commit()
    except Exception:
        db.rollback()
        raise


def write_status(db, code, product_row_id, status, error):
    """error/gone 반영 — 상태 컬럼만. 이미지도 건드리지 않는다(기존 done 데이터 보존)."""
    db.ping(reconnect=True)
    try:
        with db.cursor() as cur:
            cur.execute(_STATUS_SQL[code], {'fk': product_row_id, 'status': status,
                                            'error': (error or '')[:MAX_ERROR_LEN] or None})
        db.commit()
    except Exception:
        db.rollback()
        raise
