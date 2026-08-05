"""maintenance.py(3단계 C2) — 컴팩션의 last-wins 보존, 로테이션, 아카이브 순서/범위."""
import gzip
import json
import datetime

import pytest

import gonggu.maintenance as mt
from gonggu.common import load_jsonl


def _lines(path):
    return [l for l in path.read_text(encoding='utf-8').splitlines() if l.strip()]


class TestCompact:
    def test_last_wins_preserved(self, tmp_path):
        p = tmp_path / 'cp.jsonl'
        rows = [{'key': 'a', 'v': 1}, {'key': 'b', 'v': 1}, {'key': 'a', 'v': 2}, {'key': 'a', 'v': 3}]
        p.write_text('\n'.join(json.dumps(r) for r in rows) + '\n', encoding='utf-8')
        before_view = load_jsonl(p)
        assert mt.compact_jsonl(p, min_bytes=0) == (4, 2)
        assert load_jsonl(p) == before_view          # 소비자 관점에서 완전히 동일
        assert len(_lines(p)) == 2

    def test_below_threshold_untouched(self, tmp_path):
        p = tmp_path / 'cp.jsonl'
        p.write_text('{"key": "a"}\n', encoding='utf-8')
        assert mt.compact_jsonl(p, min_bytes=10_000_000) is None

    def test_missing_file(self, tmp_path):
        assert mt.compact_jsonl(tmp_path / 'nope.jsonl', 0) is None


class TestRotateUsage:
    def test_old_entries_moved_to_monthly_archive(self, tmp_path, monkeypatch):
        usage = tmp_path / 'llm_usage.jsonl'
        arch = tmp_path / 'llm_usage_archive'
        monkeypatch.setattr(mt, 'USAGE_FILE', usage)
        monkeypatch.setattr(mt, 'USAGE_ARCHIVE_DIR', arch)
        rows = [{'ts': '2026-06-15T10:00:00', 'model': 'x'},
                {'ts': '2026-07-01T10:00:00', 'model': 'x'},
                {'ts': '2026-08-04T10:00:00', 'model': 'x'}]
        usage.write_text('\n'.join(json.dumps(r) for r in rows) + '\n', encoding='utf-8')
        moved, kept = mt.rotate_usage(30, today=datetime.date(2026, 8, 5))
        assert (moved, kept) == (2, 1)
        assert len(_lines(usage)) == 1
        assert len(_lines(arch / '2026-06.jsonl')) == 1
        assert len(_lines(arch / '2026-07.jsonl')) == 1


class TestArchive:
    @pytest.fixture
    def env(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mt, 'ROOT', tmp_path)
        monkeypatch.setattr(mt, 'ARCHIVE_ROOT', tmp_path / 'data/archive')
        for d in mt.ARCHIVE_DIRS:
            (tmp_path / 'data' / d).mkdir(parents=True)
        return tmp_path

    def test_old_dates_gzipped_and_removed(self, env):
        old = env / 'data/01_raw/2026-06-01.jsonl'
        recent = env / 'data/01_raw/2026-08-01.jsonl'
        old.write_text('{"post_id": "P1"}\n', encoding='utf-8')
        recent.write_text('{"post_id": "P2"}\n', encoding='utf-8')
        moved = mt.archive_old_dates(30, today=datetime.date(2026, 8, 5))
        assert len(moved) == 1
        assert not old.exists() and recent.exists()
        gz = env / 'data/archive/01_raw/2026-06-01.jsonl.gz'
        assert gz.exists()
        assert gzip.open(gz, 'rt', encoding='utf-8').read() == '{"post_id": "P1"}\n'

    def test_unknown_bucket_never_archived(self, env):
        f = env / 'data/02_classified/_unknown.jsonl'
        f.write_text('{"x": 1}\n', encoding='utf-8')
        assert mt.archive_old_dates(1, today=datetime.date(2026, 8, 5)) == []
        assert f.exists()

    def test_raw_before_classified_order(self, env):
        """01[d]만 남고 02[d]만 사라지는 순간이 없도록 01이 먼저 옮겨진다(재분류 폭탄 방지)."""
        for d in ('01_raw', '02_classified'):
            (env / f'data/{d}/2026-06-01.jsonl').write_text('{}\n', encoding='utf-8')
        moved = mt.archive_old_dates(30, today=datetime.date(2026, 8, 5))
        assert [src.parent.name for src, _ in moved] == ['01_raw', '02_classified']
