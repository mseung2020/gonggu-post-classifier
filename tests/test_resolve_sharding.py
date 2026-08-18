"""리졸브 워커 풀 프로세스 샤딩(2026-08-18, 속도개선 다음 라운드 E) 계약:
- runner._shard_index: 같은 key는 항상 같은 샤드로(결정론적), n개 샤드에 대체로 고르게 분산.
- daily._split_evenly: 총량을 n개로 나눠도 합이 원래 총량과 같고 각 항목 최소 1.
- daily._run_resolve_links_sharded: 전 샤드 성공해야만 --finalize를 부르고, 하나라도 실패하면
  그 샤드의 실패 코드를 그대로 반환하며 --finalize는 건너뛴다(다른 샤드가 모르는 불완전한
  결과로 RESOLVED_DIR을 잘못 재조립하지 않기 위함 — runner.finalize() docstring 참고).
실제 서브프로세스/Playwright 없이 _run_stage_with_stall_retry/_run_stage를 페이크로 바꿔
오케스트레이션 로직만 검증한다."""
import io

import gonggu.daily as daily
from gonggu.resolve_links.runner import _shard_index


class _FakeLog(io.StringIO):
    pass


class TestShardIndex:
    def test_same_key_always_same_shard(self):
        assert _shard_index('ig:abc:0', 5) == _shard_index('ig:abc:0', 5)

    def test_within_range(self):
        for key in ['ig:abc:0', 'yt:def:1', 'ig:Db9udgSPntL:72']:
            assert 0 <= _shard_index(key, 3) < 3

    def test_distributes_across_shards(self):
        """수백 개의 서로 다른 key를 넣으면 한 샤드에 다 몰리지 않아야 한다(완벽한 균등은
        아니어도, crc32 해시가 최소한 여러 샤드에 걸쳐 나뉘는지만 확인)."""
        buckets = {0: 0, 1: 0, 2: 0, 3: 0}
        for i in range(400):
            buckets[_shard_index(f'ig:post{i}:{i % 3}', 4)] += 1
        assert all(count > 0 for count in buckets.values())
        assert max(buckets.values()) < 400  # 전부 한 샤드로 쏠리지 않음

    def test_single_shard_always_zero(self):
        assert _shard_index('anything', 1) == 0


class TestSplitEvenly:
    def test_exact_division(self):
        assert daily._split_evenly(12, 3) == [4, 4, 4]

    def test_remainder_goes_to_first_shards(self):
        assert daily._split_evenly(14, 3) == [5, 5, 4]
        assert sum(daily._split_evenly(14, 3)) == 14

    def test_minimum_one_per_shard(self):
        # 샤드 수가 총량보다 많아도 각 항목은 최소 1(총합이 total을 넘어설 수 있음 — 극단값 방어)
        assert all(x >= 1 for x in daily._split_evenly(2, 5))


class TestRunResolveLinksSharded:
    def _patch_success(self, monkeypatch, calls, finalize_calls):
        def fake_stall_retry(module, extra_env, log, retry_limit=None, extra_args=(),
                              log_lock=None, tag=''):
            calls.append(dict(extra_env))
            return 0, [], 1.0, 0

        def fake_run_stage(module, extra_env, log, extra_args=(), log_lock=None, tag=''):
            finalize_calls.append({'extra_args': extra_args, 'tag': tag})
            return 0, ['finalize-ok']

        monkeypatch.setattr(daily, '_run_stage_with_stall_retry', fake_stall_retry)
        monkeypatch.setattr(daily, '_run_stage', fake_run_stage)

    def test_all_shards_succeed_then_finalizes_once(self, monkeypatch):
        calls, finalize_calls = [], []
        self._patch_success(monkeypatch, calls, finalize_calls)
        code, stderr_tail, dt, retries = daily._run_resolve_links_sharded(
            'resolve_links', {'MAX_BROWSERS': '14', 'RESOLVE_CONCURRENCY': '60'}, _FakeLog(), 3)
        assert code == 0
        assert stderr_tail == ['finalize-ok']
        assert len(calls) == 3
        assert len(finalize_calls) == 1
        assert finalize_calls[0]['extra_args'] == ('--finalize',)

    def test_shard_browsers_and_workers_sum_to_original_total(self, monkeypatch):
        calls, finalize_calls = [], []
        self._patch_success(monkeypatch, calls, finalize_calls)
        daily._run_resolve_links_sharded(
            'resolve_links', {'MAX_BROWSERS': '14', 'RESOLVE_CONCURRENCY': '60'}, _FakeLog(), 3)
        total_browsers = sum(int(c['MAX_BROWSERS']) for c in calls)
        total_workers = sum(int(c['RESOLVE_CONCURRENCY']) for c in calls)
        assert total_browsers == 14  # 샤드 3개가 나눠 가져도 총합은 원래 값 그대로(RAM 안전선 유지)
        assert total_workers == 60
        # 각 샤드가 자기 인덱스를 알아야 pending을 정확히 나눠 가져간다
        assert sorted(int(c['RESOLVE_SHARD_INDEX']) for c in calls) == [0, 1, 2]
        assert all(c['RESOLVE_SHARD_COUNT'] == '3' for c in calls)

    def test_shard_fast_concurrency_and_max_per_domain_also_sum_to_original_total(self, monkeypatch):
        """문제 5 회귀 테스트(2026-08-18) — RESOLVE_FAST_CONCURRENCY(Tier0 동시성)와
        MAX_PER_DOMAIN(도메인당 동시 접근 상한)도 MAX_BROWSERS/RESOLVE_CONCURRENCY와 똑같이
        샤드 수만큼 나눠야 한다. 안 나누면 각 샤드가 원래 값을 그대로 복제해서, Tier0 총
        동시요청과 같은 도메인에 대한 실질 동시 접근이 샤드 수배로 뛴다."""
        calls, finalize_calls = [], []
        self._patch_success(monkeypatch, calls, finalize_calls)
        daily._run_resolve_links_sharded(
            'resolve_links',
            {'MAX_BROWSERS': '14', 'RESOLVE_CONCURRENCY': '60',
             'RESOLVE_FAST_CONCURRENCY': '200', 'MAX_PER_DOMAIN': '4'},
            _FakeLog(), 3)
        assert sum(int(c['RESOLVE_FAST_CONCURRENCY']) for c in calls) == 200
        assert sum(int(c['MAX_PER_DOMAIN']) for c in calls) == 4
        # 각 값이 최소 1은 되어야 한다(0으로 나뉘면 그 샤드는 사실상 일을 못 함)
        assert all(int(c['MAX_PER_DOMAIN']) >= 1 for c in calls)

    def test_shard_fast_concurrency_and_max_per_domain_use_config_defaults_when_unset(self, monkeypatch):
        """extra_env/os.environ 어디에도 없으면 config.py의 실제 기본값(200/4)을 기준으로
        나눠야 한다 — 0이나 다른 값을 기본으로 잘못 잡으면 사용자가 아무 설정도 안 했을 때
        (daily.py 기본 실행) 조용히 다른 동작을 하게 된다."""
        calls, finalize_calls = [], []
        self._patch_success(monkeypatch, calls, finalize_calls)
        daily._run_resolve_links_sharded(
            'resolve_links', {'MAX_BROWSERS': '14', 'RESOLVE_CONCURRENCY': '60'}, _FakeLog(), 4)
        assert sum(int(c['RESOLVE_FAST_CONCURRENCY']) for c in calls) == 200
        assert sum(int(c['MAX_PER_DOMAIN']) for c in calls) == 4

    def test_one_shard_failure_skips_finalize(self, monkeypatch):
        finalize_calls = []

        def fake_stall_retry(module, extra_env, log, retry_limit=None, extra_args=(),
                              log_lock=None, tag=''):
            if extra_env['RESOLVE_SHARD_INDEX'] == '1':
                return 3, ['shard-1-stalled-forever'], 5.0, 6
            return 0, [], 1.0, 0

        def fake_run_stage(*a, **k):
            finalize_calls.append(True)
            return 0, ['should-not-be-called']

        monkeypatch.setattr(daily, '_run_stage_with_stall_retry', fake_stall_retry)
        monkeypatch.setattr(daily, '_run_stage', fake_run_stage)

        code, stderr_tail, dt, retries = daily._run_resolve_links_sharded(
            'resolve_links', {'MAX_BROWSERS': '14', 'RESOLVE_CONCURRENCY': '60'}, _FakeLog(), 3)
        assert code == 3
        assert finalize_calls == []  # 실패한 샤드가 있으면 finalize를 아예 안 부름
        assert any('shard-1-stalled-forever' in line for line in stderr_tail)

    def test_retries_summed_across_shards(self, monkeypatch):
        def fake_stall_retry(module, extra_env, log, retry_limit=None, extra_args=(),
                              log_lock=None, tag=''):
            idx = int(extra_env['RESOLVE_SHARD_INDEX'])
            return 0, [], 1.0, idx  # 샤드 i는 i회 재시도했다고 가정(0+1+2=3)

        monkeypatch.setattr(daily, '_run_stage_with_stall_retry', fake_stall_retry)
        monkeypatch.setattr(daily, '_run_stage', lambda *a, **k: (0, []))

        _, _, _, retries = daily._run_resolve_links_sharded(
            'resolve_links', {'MAX_BROWSERS': '14', 'RESOLVE_CONCURRENCY': '60'}, _FakeLog(), 3)
        assert retries == 0 + 1 + 2
