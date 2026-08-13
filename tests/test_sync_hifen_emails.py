"""sync_hifen_emails.py — HIFEN_EMAIL_FILE을 hifen.instagram_user.email에 반영하는 로직.
실제 DB 연결은 fake connection으로 대체해서 UPDATE 문/rowcount 집계만 검증한다."""
import json

from gonggu import sync_hifen_emails
from gonggu.common import HIFEN_EMAIL_FILE


class _FakeCursor:
    def __init__(self, rowcounts):
        self._rowcounts = rowcounts  # user_id -> rowcount
        self.executed = []
        self.rowcount = 0

    def execute(self, sql, params):
        self.executed.append((sql, params))
        self.rowcount = self._rowcounts.get(params[1], 0)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, rowcounts):
        self.cursor_obj = _FakeCursor(rowcounts)
        self.committed = False

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.committed = True

    def close(self):
        pass


def _write_email_file(records):
    HIFEN_EMAIL_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(HIFEN_EMAIL_FILE, 'w', encoding='utf-8') as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + '\n')


def test_sync_counts_changed_rows_only(monkeypatch):
    before = HIFEN_EMAIL_FILE.read_text() if HIFEN_EMAIL_FILE.exists() else None
    try:
        _write_email_file([
            {'key': 'user_a', 'user_id': 'user_a', 'emails': ['a@x.com', 'b@x.com']},
            {'key': 'user_b', 'user_id': 'user_b', 'emails': ['c@x.com']},
        ])
        # user_a는 실제로 바뀜(rowcount=1), user_b는 hifen에 이미 같은 값이라 rowcount=0
        fake_conn = _FakeConn({'user_a': 1, 'user_b': 0})
        monkeypatch.setattr(sync_hifen_emails, 'connect_src', lambda: fake_conn)
        monkeypatch.setattr(sync_hifen_emails, 'acquire_lock', lambda name: None)

        sync_hifen_emails.main()

        assert fake_conn.committed
        params = [p for _, p in fake_conn.cursor_obj.executed]
        assert ('a@x.com,b@x.com', 'user_a') in params
        assert ('c@x.com', 'user_b') in params
    finally:
        if before is None:
            HIFEN_EMAIL_FILE.unlink(missing_ok=True)
        else:
            HIFEN_EMAIL_FILE.write_text(before)


def test_sync_skips_records_without_emails(monkeypatch):
    before = HIFEN_EMAIL_FILE.read_text() if HIFEN_EMAIL_FILE.exists() else None
    try:
        _write_email_file([{'key': 'user_c', 'user_id': 'user_c', 'emails': []}])
        fake_conn = _FakeConn({})
        monkeypatch.setattr(sync_hifen_emails, 'connect_src', lambda: fake_conn)
        monkeypatch.setattr(sync_hifen_emails, 'acquire_lock', lambda name: None)

        sync_hifen_emails.main()

        assert fake_conn.cursor_obj.executed == []
    finally:
        if before is None:
            HIFEN_EMAIL_FILE.unlink(missing_ok=True)
        else:
            HIFEN_EMAIL_FILE.write_text(before)
