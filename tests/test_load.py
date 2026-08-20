"""load.py — 입력 병합/보류 규칙(감사 A3)과 중복 경합 판정(감사 A4)."""
import pymysql

from gonggu.load import _is_duplicate_entry, _item_key, split_unresolved
from gonggu.platforms import PLATFORMS


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


class _FakeCursor:
    """load_all 검증용 — platforms가 생성한 SQL 문자열을 보고 존재확인/INSERT를 흉내낸다."""

    def __init__(self, db):
        self.db = db
        self._found = False
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        if sql.startswith('SELECT id FROM'):
            self.db.exists_queries += 1
            self._found = params[0] in self.db.existing or params[0] in self.db.staged
        elif sql.startswith('SELECT post_id FROM') or sql.startswith('SELECT video_id FROM'):
            # 자연키 일괄 조회(2026-08-20) — 테스트 항목은 전부 'ig'라 yt 테이블은 비어 있다.
            self.db.bulk_key_queries += 1
            col = 'post_id' if 'gonggu_post' in sql else 'video_id'
            self._rows = ([{col: k} for k in sorted(self.db.existing)]
                          if col == 'post_id' else [])
        elif '_product' in sql.split('(')[0]:
            pass  # 상품 INSERT — 부모가 성공했으면 항상 성공한다고 가정
        elif sql.startswith('INSERT INTO'):
            key = params['post_id'] if 'gonggu_post' in sql else params['video_id']
            if key in self.db.fail_keys:
                raise ValueError('Data too long for column')
            if key in self.db.race_dup_keys:
                raise pymysql.err.IntegrityError(1062, f"Duplicate entry '{key}'")
            self.db.staged.add(key)

    def fetchone(self):
        return {'id': 1} if self._found else None

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, existing=(), fail_keys=(), race_dup_keys=()):
        self.existing = set(existing)      # 커밋된 키
        self.staged = set()                # 이번 트랜잭션에서 INSERT된 키
        self.fail_keys = set(fail_keys)    # INSERT가 일반 오류로 터지는 키
        self.race_dup_keys = set(race_dup_keys)  # 경합(1062)을 흉내내는 키
        self.commits = 0
        self.exists_queries = 0    # 건별 존재확인 왕복 횟수
        self.bulk_key_queries = 0  # 자연키 일괄 조회 횟수

    def cursor(self):
        return _FakeCursor(self)

    def commit(self):
        self.existing |= self.staged
        self.staged.clear()
        self.commits += 1

    def rollback(self):
        self.staged.clear()


class TestLoadAllBatching:
    """4단계 D2 — 소배치 커밋 + 실패 배치 건별 폴백."""

    def _items(self, keys):
        return [_item('ig', k) for k in keys]

    def test_happy_path_one_commit_per_batch(self):
        from gonggu.load import load_all
        conn = _FakeConn()
        inserted, skipped, failed = load_all(conn, self._items(['A', 'B', 'C', 'D', 'E']), batch_size=2)
        assert (inserted, skipped, failed) == (5, 0, 0)
        assert conn.commits == 3               # 2+2+1 → 커밋 3회(예전엔 5회)
        assert conn.existing == {'A', 'B', 'C', 'D', 'E'}

    def test_existing_counted_as_skipped(self):
        from gonggu.load import load_all
        conn = _FakeConn(existing=['B'])
        inserted, skipped, failed = load_all(conn, self._items(['A', 'B', 'C']), batch_size=50)
        assert (inserted, skipped, failed) == (2, 1, 0)

    def test_bad_item_isolated_by_per_item_fallback(self):
        """배치 중간의 실패가 같은 배치의 다른 건을 못 막는다 — 기존 건별 커밋의 보장 유지."""
        from gonggu.load import load_all
        conn = _FakeConn(fail_keys=['B'])
        inserted, skipped, failed = load_all(conn, self._items(['A', 'B', 'C']), batch_size=50)
        assert (inserted, skipped, failed) == (2, 0, 1)
        assert conn.existing == {'A', 'C'}     # 롤백 후 폴백에서 A/C 재적재됨

    def test_race_duplicate_counted_as_skip(self):
        from gonggu.load import load_all
        conn = _FakeConn(race_dup_keys=['B'])
        inserted, skipped, failed = load_all(conn, self._items(['A', 'B']), batch_size=50)
        assert (inserted, skipped, failed) == (1, 1, 0)

    def test_batch_size_one_behaves_like_legacy(self):
        from gonggu.load import load_all
        conn = _FakeConn(fail_keys=['B'])
        inserted, skipped, failed = load_all(conn, self._items(['A', 'B', 'C']), batch_size=1)
        assert (inserted, skipped, failed) == (2, 0, 1)
        assert conn.existing == {'A', 'C'}


class TestExistingKeysCache:
    """존재확인 일괄 조회(2026-08-20) — 예전엔 항목마다 `SELECT ... WHERE post_id=%s`를 던졌다.
    이 단계는 매일 04_resolved **전체**(누적)를 훑으며 그날 새 것만 넣는 구조라 대부분이
    "이미 있음" 확인에만 쓰이는 왕복이었다: 실측 45,566건 중 43,737건(96%)이 스킵이었고
    존재확인 1건이 6.06ms라 그 왕복만 약 276초. 같은 키를 한 번에 가져오면 0.16초다."""

    def _items(self, keys):
        return [_item('ig', k) for k in keys]

    def test_no_per_item_exists_query_on_happy_path(self):
        from gonggu.load import load_all
        conn = _FakeConn(existing=['B'])
        load_all(conn, self._items(['A', 'B', 'C', 'D']), batch_size=2)
        assert conn.bulk_key_queries == len(PLATFORMS)   # 플랫폼당 한 번씩만
        assert conn.exists_queries == 0, '정상 경로에서 건별 존재확인이 남아 있다'

    def test_same_key_twice_in_input_inserts_once(self):
        """메모리 집합을 쓰면 "방금 넣은 것"을 스스로 기억해야 한다 — 안 그러면 같은 키가
        두 번 들어간 입력에서 UNIQUE 충돌이 나고 배치가 통째로 롤백된다."""
        from gonggu.load import load_all
        conn = _FakeConn()
        inserted, skipped, failed = load_all(conn, self._items(['A', 'A', 'B']), batch_size=50)
        assert (inserted, skipped, failed) == (2, 1, 0)
        assert conn.existing == {'A', 'B'}

    def test_rollback_restores_the_key_set(self):
        """배치가 롤백되면 DB는 되돌아가는데 메모리 집합만 "넣었다"로 남으면 그 건을 영영
        스킵하게 된다 — 롤백 시 집합도 배치 시작 시점으로 되돌려야 한다."""
        from gonggu.load import load_all
        conn = _FakeConn(fail_keys=['B'])
        inserted, skipped, failed = load_all(conn, self._items(['A', 'B', 'C']), batch_size=50)
        assert (inserted, skipped, failed) == (2, 0, 1)
        assert conn.existing == {'A', 'C'}, 'A/C가 폴백에서 재적재되지 않았다'

    def test_key_set_updated_after_failed_batch_fallback(self):
        """폴백으로 실제 들어간 것들이 집합에 반영돼야, 뒤 배치가 같은 키를 또 넣으려 하지 않는다."""
        from gonggu.load import load_all
        conn = _FakeConn(fail_keys=['B'])
        # 1번 배치(A,B) 실패 → 폴백에서 A 적재. 2번 배치에 A가 또 나와도 스킵돼야 한다.
        inserted, skipped, failed = load_all(conn, self._items(['A', 'B', 'A', 'C']), batch_size=2)
        assert failed == 1 and conn.existing == {'A', 'C'}
        assert inserted == 2 and skipped == 1

    def test_existing_keys_reads_every_platform(self):
        from gonggu.load import existing_keys
        conn = _FakeConn(existing=['P1'])
        with conn.cursor() as cur:
            keys = existing_keys(cur)
        assert ('ig', 'P1') in keys
        assert conn.bulk_key_queries == len(PLATFORMS)

    def test_load_item_without_known_still_queries_db(self):
        """known=None이면 예전 동작(건건이 DB에 묻기) — 단건 폴백 경로가 이걸 쓴다."""
        from gonggu.load import load_item
        conn = _FakeConn(existing=['P1'])
        with conn.cursor() as cur:
            assert load_item(cur, 'ig', {'post_id': 'P1'}, []) is False
        assert conn.exists_queries == 1
