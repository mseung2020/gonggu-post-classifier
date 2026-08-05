"""platforms.py — 메타테이블에서 생성한 SQL이 리팩터링 전(2단계 B4 이전)의 문자열과
의미상 동일한지 못박는다. 레거시 문자열은 당시 load.py/rescan_inprogress.py/
backfill_period.py에서 그대로 복사해온 것 — 이 테스트가 깨지면 생성 로직이 원래 SQL과
달라졌다는 뜻이니 메타/빌더를 고칠 것(레거시 쪽을 고치는 게 아님)."""
import re

from gonggu.platforms import (PLATFORMS, native_id, parent_ctx_from_row, parent_exists_sql,
                              parent_insert_sql, parent_update_period_sql, product_insert_sql,
                              product_update_link_sql)


def norm(sql):
    return re.sub(r'\s+', ' ', sql).strip()


LEGACY_INSERT_VIDEO = """
INSERT INTO gonggu_video
    (video_id, channel_id, title, video_url, external_url, publishDate, gonggu_start_date,
     gonggu_end_date, gonggu_stage, classification_note)
VALUES (%(video_id)s, %(channel_id)s, %(title)s, %(video_url)s, %(external_url)s, %(publishDate)s,
        %(gonggu_start_date)s, %(gonggu_end_date)s, %(gonggu_stage)s, %(classification_note)s)
"""
LEGACY_INSERT_POST = """
INSERT INTO gonggu_post
    (post_id, user_id, url, publish_date, gonggu_start_date, gonggu_end_date, gonggu_stage,
     classification_note)
VALUES (%(post_id)s, %(user_id)s, %(url)s, %(publish_date)s,
        %(gonggu_start_date)s, %(gonggu_end_date)s, %(gonggu_stage)s, %(classification_note)s)
"""
LEGACY_INSERT_VIDEO_PRODUCT = """
INSERT INTO gonggu_video_product
    (video_id, product_name, link_location, url_type, candidate_url, link_status, sort_order)
VALUES (%(video_id)s, %(product_name)s, %(link_location)s, %(url_type)s, %(candidate_url)s,
        %(link_status)s, %(sort_order)s)
"""
LEGACY_INSERT_POST_PRODUCT = """
INSERT INTO gonggu_post_product
    (post_id, product_name, link_location, url_type, candidate_url, link_status, sort_order)
VALUES (%(post_id)s, %(product_name)s, %(link_location)s, %(url_type)s, %(candidate_url)s,
        %(link_status)s, %(sort_order)s)
"""


class TestLoadSql:
    def test_parent_insert(self):
        assert norm(parent_insert_sql(PLATFORMS['yt'])) == norm(LEGACY_INSERT_VIDEO)
        assert norm(parent_insert_sql(PLATFORMS['ig'])) == norm(LEGACY_INSERT_POST)

    def test_parent_exists(self):
        assert parent_exists_sql(PLATFORMS['yt']) == 'SELECT id FROM gonggu_video WHERE video_id = %s'
        assert parent_exists_sql(PLATFORMS['ig']) == 'SELECT id FROM gonggu_post WHERE post_id = %s'

    def test_product_insert(self):
        assert norm(product_insert_sql(PLATFORMS['yt'])) == norm(LEGACY_INSERT_VIDEO_PRODUCT)
        assert norm(product_insert_sql(PLATFORMS['ig'])) == norm(LEGACY_INSERT_POST_PRODUCT)


class TestEnrichSql:
    def test_rescan_product_update(self):
        assert product_update_link_sql(PLATFORMS['ig']) == (
            'UPDATE gonggu_post_product SET candidate_url = %s, link_status = %s, updated_at = NOW() WHERE id = %s')
        assert product_update_link_sql(PLATFORMS['yt']) == (
            'UPDATE gonggu_video_product SET candidate_url = %s, link_status = %s, updated_at = NOW() WHERE id = %s')

    def test_backfill_parent_update(self):
        assert parent_update_period_sql(PLATFORMS['ig']) == (
            'UPDATE gonggu_post SET gonggu_start_date=%s, gonggu_end_date=%s, gonggu_stage=%s WHERE post_id=%s')
        assert parent_update_period_sql(PLATFORMS['yt']) == (
            'UPDATE gonggu_video SET gonggu_start_date=%s, gonggu_end_date=%s, gonggu_stage=%s WHERE video_id=%s')


class TestHelpers:
    def test_native_id(self):
        assert native_id('ig', {'post_id': 'P1'}) == 'P1'
        assert native_id('yt', {'video_id': 'V1'}) == 'V1'

    def test_parent_ctx_from_row_ig(self):
        import datetime
        row = {'post_id': 'P1', 'user_id': 'u', 'url': 'https://x',
               'publish_date': datetime.datetime(2026, 8, 1, 10, 0), 'classification_note': 'n',
               'row_id': 9, 'product_name': '냄비'}
        ctx = parent_ctx_from_row(PLATFORMS['ig'], row)
        assert ctx == {'post_id': 'P1', 'user_id': 'u', 'url': 'https://x',
                       'publish_date': '2026-08-01 10:00:00', 'classification_note': 'n'}

    def test_parent_ctx_from_row_yt_date_stringified(self):
        import datetime
        row = {'video_id': 'V1', 'channel_id': 'c', 'video_url': 'https://y',
               'publishDate': datetime.date(2026, 8, 1), 'classification_note': None}
        ctx = parent_ctx_from_row(PLATFORMS['yt'], row)
        assert ctx['publishDate'] == '2026-08-01' and ctx['classification_note'] is None
