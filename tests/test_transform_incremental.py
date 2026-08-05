"""transform 증분 모드(3단계 C1) — 변경 감지·부분 재계산·상태 파일 계약을 박제한다."""
import json

import pytest

import gonggu.transform as tr


def _rec(post_id, date, is_gonggu=True):
    return {'platform': 'ig', 'post_id': post_id, 'user_id': 'u', 'url': 'https://x',
            'publish_date': f'{date} 10:00:00', 'description': '공구 오픈',
            'classification': {'is_gonggu': is_gonggu,
                               'products': [{'name': '냄비', 'urls': ['https://litt.ly/a']}]}
            if is_gonggu is not None else None}


def _write_date_file(dir_path, date, records):
    dir_path.mkdir(parents=True, exist_ok=True)
    with open(dir_path / f'{date}.jsonl', 'w', encoding='utf-8') as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + '\n')


@pytest.fixture
def env(tmp_path, monkeypatch):
    classified = tmp_path / '02_classified'
    ready = tmp_path / '03_load_ready'
    state = tmp_path / 'transform_state.json'
    monkeypatch.setattr(tr, 'CLASSIFIED_DIR', classified)
    monkeypatch.setattr(tr, 'LOAD_READY_DIR', ready)
    monkeypatch.setattr(tr, 'STATE_FILE', state)
    monkeypatch.setenv('GONGGU_TODAY', '2026-08-05')
    monkeypatch.setattr(tr.sys, 'argv', ['transform'])
    return classified, ready, state


class TestChangedDates:
    def test_new_and_modified_detected(self):
        meta = {'a': [1, 10], 'b': [2, 20], 'c': [3, 30]}
        state = {'a': [1, 10], 'b': [1, 20]}          # b는 mtime 변경, c는 신규
        assert sorted(tr.changed_dates(meta, state)) == ['b', 'c']

    def test_archived_date_not_targeted(self):
        # state에는 있는데 파일이 사라진(아카이브된) 날짜는 대상에 안 잡힘
        assert tr.changed_dates({'a': [1, 10]}, {'a': [1, 10], 'gone': [9, 9]}) == []


class TestIncrementalMain:
    def test_first_run_processes_all_then_noop(self, env, capsys):
        classified, ready, state = env
        _write_date_file(classified, '2026-08-01', [_rec('P1', '2026-08-01')])
        _write_date_file(classified, '2026-08-02', [_rec('P2', '2026-08-02', is_gonggu=False)])

        tr.main()
        assert (ready / '2026-08-01.jsonl').exists()
        assert (ready / '2026-08-02.jsonl').exists()  # 0건이어도 빈 파일로 교체(stale 방지)
        assert (ready / '2026-08-02.jsonl').read_text() == ''
        assert json.loads((ready / '2026-08-01.jsonl').read_text())['parent']['post_id'] == 'P1'

        capsys.readouterr()
        tr.main()  # 아무것도 안 바뀜 → no-op
        assert '변경 없음' in capsys.readouterr().out

    def test_only_changed_date_recomputed(self, env):
        classified, ready, state = env
        _write_date_file(classified, '2026-08-01', [_rec('P1', '2026-08-01')])
        _write_date_file(classified, '2026-08-02', [_rec('P2', '2026-08-02')])
        tr.main()
        # 8/1의 03 파일에 표식을 남기고, 8/2만 변경 — 8/1은 건드리면 안 됨
        marker = ready / '2026-08-01.jsonl'
        original = marker.read_text()
        marker.write_text(original + '\n# marker\n')
        _write_date_file(classified, '2026-08-02',
                         [_rec('P2', '2026-08-02'), _rec('P3', '2026-08-02')])
        tr.main()
        assert '# marker' in marker.read_text()                       # 8/1 그대로
        lines = [l for l in (ready / '2026-08-02.jsonl').read_text().splitlines() if l.strip()]
        assert len(lines) == 2                                        # 8/2는 재계산됨

    def test_full_flag_rewrites_everything(self, env, monkeypatch):
        classified, ready, state = env
        _write_date_file(classified, '2026-08-01', [_rec('P1', '2026-08-01')])
        tr.main()
        marker = ready / '2026-08-01.jsonl'
        marker.write_text(marker.read_text() + '\n# marker\n')
        monkeypatch.setattr(tr.sys, 'argv', ['transform', '--full'])
        tr.main()
        assert '# marker' not in marker.read_text()                   # 전체 재작성으로 사라짐

    def test_corrupt_state_falls_back_to_full_scan(self, env):
        classified, ready, state = env
        _write_date_file(classified, '2026-08-01', [_rec('P1', '2026-08-01')])
        state.write_text('{{{corrupt', encoding='utf-8')
        tr.main()  # 죽지 않고 전체를 변경으로 간주
        assert (ready / '2026-08-01.jsonl').exists()
