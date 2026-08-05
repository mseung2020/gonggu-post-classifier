"""load.py — 입력 병합/보류 규칙(감사 A3)과 중복 경합 판정(감사 A4)."""
import pymysql

from gonggu.load import _is_duplicate_entry, _item_key, split_unresolved


def _item(platform, native_id):
    id_field = 'post_id' if platform == 'ig' else 'video_id'
    return {'platform': platform, 'parent': {id_field: native_id}, 'products': []}


class TestSplitUnresolved:
    def test_items_only_in_ready_are_detected(self):
        resolved = [_item('ig', 'P1'), _item('yt', 'V1')]
        ready = [_item('ig', 'P1'), _item('yt', 'V1'), _item('ig', 'P2')]
        got_resolved, unresolved = split_unresolved(resolved, ready)
        assert got_resolved == resolved
        assert [_item_key(i) for i in unresolved] == ['ig:P2']

    def test_fresh_resolved_has_no_leftover(self):
        items = [_item('ig', 'P1'), _item('yt', 'V1')]
        _, unresolved = split_unresolved(items, items)
        assert unresolved == []

    def test_same_native_id_across_platforms_not_confused(self):
        resolved = [_item('ig', 'X1')]
        ready = [_item('ig', 'X1'), _item('yt', 'X1')]
        _, unresolved = split_unresolved(resolved, ready)
        assert [_item_key(i) for i in unresolved] == ['yt:X1']


class TestDuplicateEntry:
    def test_duplicate_errno_1062(self):
        e = pymysql.err.IntegrityError(1062, "Duplicate entry 'P1' for key 'uq_gonggu_post_post_id'")
        assert _is_duplicate_entry(e)

    def test_other_integrity_error_is_failure(self):
        e = pymysql.err.IntegrityError(1452, 'FK constraint fails')
        assert not _is_duplicate_entry(e)

    def test_non_integrity_error_is_failure(self):
        assert not _is_duplicate_entry(pymysql.err.OperationalError(2013, 'Lost connection'))
        assert not _is_duplicate_entry(ValueError('x'))
