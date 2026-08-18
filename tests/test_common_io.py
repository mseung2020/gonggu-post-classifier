"""common.py — 체크포인트 I/O 규약(append-only last-wins, 날짜 샤딩)의 박제."""
import json

import gonggu.common as common
from gonggu.common import (_date_key_from_raw, append_jsonl, dump_jsonl_sharded, is_affiliate_ranking,
                    load_classify_done_keys, load_json_dir, load_jsonl, record_classify_done_key)


class TestDateKey:
    def test_normal_and_datetime(self):
        assert _date_key_from_raw('2026-08-03') == '2026-08-03'
        assert _date_key_from_raw('2026-08-03 12:00:00') == '2026-08-03'

    def test_garbage_goes_to_unknown_bucket(self):
        assert _date_key_from_raw(None) == '_unknown'
        assert _date_key_from_raw('') == '_unknown'
        assert _date_key_from_raw('날짜아님') == '_unknown'
        assert _date_key_from_raw('2026-99-01') == '_unknown'


class TestJsonlCheckpoint:
    def test_last_wins(self, tmp_path):
        p = tmp_path / 'cp.jsonl'
        append_jsonl(p, {'key': 'a', 'status': 'unresolved'})
        append_jsonl(p, {'key': 'b', 'status': 'done'})
        append_jsonl(p, {'key': 'a', 'status': 'done'})  # 재탐색으로 갱신
        out = load_jsonl(p)
        assert out['a']['status'] == 'done' and out['b']['status'] == 'done'

    def test_missing_file_is_empty(self, tmp_path):
        assert load_jsonl(tmp_path / 'nope.jsonl') == {}

    def test_blank_lines_ignored(self, tmp_path):
        p = tmp_path / 'cp.jsonl'
        p.write_text('{"key": "a", "v": 1}\n\n\n', encoding='utf-8')
        assert load_jsonl(p)['a']['v'] == 1


class TestShardedDump:
    def test_shards_by_date_fn(self, tmp_path):
        recs = [{'d': '2026-08-01', 'i': 1}, {'d': '2026-08-02', 'i': 2}, {'d': '2026-08-01', 'i': 3}]
        dump_jsonl_sharded(tmp_path, recs, lambda r: r['d'])
        assert sorted(f.name for f in tmp_path.glob('*.jsonl')) == ['2026-08-01.jsonl', '2026-08-02.jsonl']
        lines = (tmp_path / '2026-08-01.jsonl').read_text(encoding='utf-8').strip().splitlines()
        assert [json.loads(l)['i'] for l in lines] == [1, 3]

    def test_only_keys_rewrites_selected_dates_only(self, tmp_path):
        dump_jsonl_sharded(tmp_path, [{'d': '2026-08-01', 'i': 1}], lambda r: r['d'])
        # 8/1 파일은 건드리지 않고 8/2만 새로 씀
        dump_jsonl_sharded(tmp_path, [{'d': '2026-08-02', 'i': 2}], lambda r: r['d'],
                           only_keys=['2026-08-02'])
        assert (tmp_path / '2026-08-01.jsonl').exists()
        assert (tmp_path / '2026-08-02.jsonl').exists()

    def test_load_json_dir_reads_jsonl_and_legacy_json(self, tmp_path):
        (tmp_path / '2026-08-01.jsonl').write_text('{"i": 1}\n', encoding='utf-8')
        (tmp_path / 'legacy.json').write_text('[{"i": 2}]', encoding='utf-8')
        out = load_json_dir(tmp_path)
        assert sorted(r['i'] for r in out) == [1, 2]

    def test_load_json_dir_missing(self, tmp_path):
        assert load_json_dir(tmp_path / 'nope') == []


class TestClassifyDoneKeys:
    """classify.py/classify_yt_ppl.py가 공유하는 done-키 인덱스(2026-08-18, 문제 1/9) —
    CLASSIFIED_DIR 전체를 매일 재파싱하지 않고 이 작은 파일만 보게 하는 핵심 로직."""

    def test_bootstraps_from_classified_dir_when_index_missing(self, tmp_path, monkeypatch):
        classified_dir = tmp_path / '02_classified'
        classified_dir.mkdir()
        (classified_dir / '2026-08-01.jsonl').write_text(
            '\n'.join(json.dumps(r, ensure_ascii=False) for r in [
                {'platform': 'ig', 'post_id': 'a', 'classification': {'is_gonggu': True},
                 'classification_error': None},
                {'platform': 'yt', 'video_id': 'b', 'classification': {'is_gonggu': False},
                 'classification_error': None},
                # 실패한 건은 done 인덱스에 들어가면 안 됨(재시도 대상으로 남아야 함)
                {'platform': 'ig', 'post_id': 'c', 'classification': None,
                 'classification_error': '429'},
            ]) + '\n', encoding='utf-8')
        index_file = tmp_path / 'classify_done_keys.jsonl'
        monkeypatch.setattr(common, 'CLASSIFIED_DIR', classified_dir)
        monkeypatch.setattr(common, 'CLASSIFY_DONE_KEYS_FILE', index_file)

        keys = load_classify_done_keys()
        assert keys == {'ig:a', 'yt:b'}
        assert index_file.exists()  # 부트스트랩 결과가 파일로 남아 다음부터는 재스캔 없음

    def test_reads_existing_index_without_rescanning_classified_dir(self, tmp_path, monkeypatch):
        index_file = tmp_path / 'classify_done_keys.jsonl'
        index_file.write_text('{"key": "ig:x"}\n{"key": "yt:y"}\n', encoding='utf-8')
        monkeypatch.setattr(common, 'CLASSIFY_DONE_KEYS_FILE', index_file)

        def _boom():
            raise AssertionError('인덱스 파일이 있으면 CLASSIFIED_DIR을 다시 스캔하면 안 된다')
        monkeypatch.setattr(common, '_bootstrap_classify_done_keys', _boom)

        assert load_classify_done_keys() == {'ig:x', 'yt:y'}

    def test_record_appends_a_single_key(self, tmp_path, monkeypatch):
        index_file = tmp_path / 'classify_done_keys.jsonl'
        monkeypatch.setattr(common, 'CLASSIFY_DONE_KEYS_FILE', index_file)
        record_classify_done_key('ig:new1')
        record_classify_done_key('yt:new2')
        assert set(load_jsonl(index_file).keys()) == {'ig:new1', 'yt:new2'}


class TestAffiliateRanking:
    def test_marker_plus_three_links(self):
        assert is_affiliate_ranking('쿠팡파트너스 활동으로 수수료를 제공받습니다',
                                    ['u1', 'u2', 'u3'])

    def test_marker_but_two_links_ok(self):
        assert not is_affiliate_ranking('파트너스 문구', ['u1', 'u2'])

    def test_three_links_without_marker_ok(self):
        assert not is_affiliate_ranking('그냥 공구 설명', ['u1', 'u2', 'u3'])
