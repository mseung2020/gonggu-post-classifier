"""daily.py 단계 레지스트리 계약(2026-08-20 통합) — 손으로 치던 뒷단 명령들을 STAGES에 얹으면서
튜플이 dict가 됐고 --from/--only가 id 기준이 됐다. 실제 서브프로세스 없이 순수 함수만 검증한다.

특히 못박고 싶은 것: **예전에 쓰던 --from/--only 이름이 그대로 동작하는가.** 매일 손이 기억하는
명령이라 여기가 깨지면 바로 체감된다.
"""
import pytest

import gonggu.daily as daily


class TestRegistryShape:
    def test_ids_are_unique(self):
        ids = [s['id'] for s in daily.STAGES]
        assert len(ids) == len(set(ids))

    def test_every_stage_has_id_and_known_kind(self):
        for s in daily.STAGES:
            assert s['id']
            assert s.get('kind', 'module') in ('module', 'gate')

    def test_main_line_stages_are_critical(self):
        # 본줄기 1~10은 뒤 단계가 앞 결과에 의존 — 실패하면 예전처럼 즉시 중단이어야 한다.
        for sid in ('fetch_source', 'classify', 'transform', 'resolve_links', 'load',
                    'update_gonggu_stage', 'rescan_inprogress', 'backfill_period'):
            stage = next(s for s in daily.STAGES if s['id'] == sid)
            assert daily.is_critical(stage) is True, sid

    def test_backfill_stages_are_not_critical(self):
        # 뒷단 보강은 서로 독립 — 하나가 실패해도 나머지는 돌아야 한다.
        for sid in ('sync_emails', 'uc_gate', 'reverify_uc', 'maintenance'):
            stage = next(s for s in daily.STAGES if s['id'] == sid)
            assert daily.is_critical(stage) is False, sid

    def test_maintenance_is_last(self):
        # 그날 쓴 파일을 정리하는 단계라 뒷단 보강까지 끝난 뒤에 와야 한다(2026-08-20 이동).
        assert daily.STAGES[-1]['id'] == 'maintenance'

    def test_uc_gate_precedes_every_uc_stage(self):
        ids = [s['id'] for s in daily.STAGES]
        gate_at = ids.index('uc_gate')
        for i, s in enumerate(daily.STAGES):
            if s.get('needs_uc'):
                assert i > gate_at, f"{s['id']}가 uc_gate보다 앞에 있음"

    def test_reverify_uc_carries_a_time_budget(self):
        # 안전밸브가 빠지면 uc가 죽었을 때 데일리가 통째로 묶인다(README 2026-08-12 크래시 기록).
        stage = next(s for s in daily.STAGES if s['id'] == 'reverify_uc')
        assert float(stage['env']['UC_TIME_BUDGET_SEC']) > 0


class TestStageModule:
    def test_defaults_to_id(self):
        assert daily.stage_module({'id': 'load'}) == 'load'

    def test_explicit_module_wins(self):
        assert daily.stage_module({'id': 'sync_emails', 'module': 'sync_hifen_emails'}) == 'sync_hifen_emails'

    def test_subpackage_module(self):
        stage = next(s for s in daily.STAGES if s['id'] == 'reverify_uc')
        assert daily.stage_module(stage) == 'resolve_links.reverify_uc'


class TestSelectStages:
    def test_no_flags_runs_everything(self):
        assert daily.select_stages(daily.STAGES, []) == daily.STAGES

    @pytest.mark.parametrize('name', ['resolve_links', 'load', 'rescan_inprogress',
                                      'backfill_period', 'maintenance', 'update_gonggu_stage'])
    def test_legacy_from_names_still_work(self, name):
        # 통합 전부터 쓰던 이름 — 여기가 깨지면 매일 치는 명령이 깨진다.
        out = daily.select_stages(daily.STAGES, ['--from', name])
        assert out[0]['id'] == name

    def test_from_keeps_the_tail(self):
        out = daily.select_stages(daily.STAGES, ['--from', 'load'])
        ids = [s['id'] for s in out]
        assert ids[0] == 'load' and ids[-1] == 'maintenance'
        assert 'classify' not in ids

    def test_only_picks_one(self):
        out = daily.select_stages(daily.STAGES, ['--only', 'reverify_uc'])
        assert [s['id'] for s in out] == ['reverify_uc']

    def test_from_and_only_compose(self):
        # --only는 --from이 자른 뒤에서 고른다 — 이미 잘려나간 앞쪽 단계를 되살리지 않고,
        # 남은 게 없으면 조용히 아무것도 안 하는 대신 이름이 틀렸다고 알려준다.
        with pytest.raises(SystemExit):
            daily.select_stages(daily.STAGES, ['--from', 'load', '--only', 'classify'])

    def test_from_and_only_together_when_in_range(self):
        out = daily.select_stages(daily.STAGES, ['--from', 'load', '--only', 'reverify_uc'])
        assert [s['id'] for s in out] == ['reverify_uc']

    def test_until_includes_the_named_stage(self):
        out = daily.select_stages(daily.STAGES, ['--until', 'load'])
        ids = [s['id'] for s in out]
        assert ids[0] == 'update_gonggu_stage' and ids[-1] == 'load'

    def test_from_until_makes_a_range(self):
        # 오늘의 동기(2026-08-20): 긴 무인 구간만 먼저 돌리고 사람 붙는 구간은 나중에.
        out = daily.select_stages(daily.STAGES, ['--from', 'rescan_inprogress',
                                                 '--until', 'backfill_period'])
        assert [s['id'] for s in out] == ['rescan_inprogress', 'backfill_period']

    def test_from_until_same_stage_is_one(self):
        out = daily.select_stages(daily.STAGES, ['--from', 'load', '--until', 'load'])
        assert [s['id'] for s in out] == ['load']

    def test_until_before_from_is_rejected_with_a_clear_reason(self):
        # 조용히 빈 목록을 돌려주면 "아무것도 안 했는데 성공"으로 보인다.
        with pytest.raises(SystemExit) as e:
            daily.select_stages(daily.STAGES, ['--from', 'load', '--until', 'classify'])
        assert '앞에 있습니다' in str(e.value)

    def test_unknown_until_name_exits(self):
        with pytest.raises(SystemExit):
            daily.select_stages(daily.STAGES, ['--until', 'nope'])

    def test_until_then_only(self):
        out = daily.select_stages(daily.STAGES, ['--until', 'load', '--only', 'transform'])
        assert [s['id'] for s in out] == ['transform']

    def test_unknown_from_name_exits(self):
        with pytest.raises(SystemExit):
            daily.select_stages(daily.STAGES, ['--from', 'nope'])

    def test_unknown_only_name_exits(self):
        with pytest.raises(SystemExit):
            daily.select_stages(daily.STAGES, ['--only', 'nope'])


class TestDescribe:
    def test_gate_line_mentions_no_module(self):
        stage = next(s for s in daily.STAGES if s['id'] == 'uc_gate')
        assert 'python3 -m' not in daily._describe(stage)

    def test_flags_are_shown(self):
        stage = next(s for s in daily.STAGES if s['id'] == 'reverify_uc')
        line = daily._describe(stage)
        assert '실패해도 계속' in line and 'uc 게이트 필요' in line


# ── main() 제어흐름(2026-08-20) ───────────────────────────────────────────────
# 통합의 핵심은 "어떤 단계가 실패했을 때 무엇이 계속 도는가"라서, 레지스트리 모양만으로는
# 부족하고 루프 자체를 한 번 돌려봐야 한다. 서브프로세스와 uc 게이트를 페이크로 바꿔 검증한다.

class _Harness:
    """daily.main()을 실제 서브프로세스/크롬 없이 돌린다. 어떤 단계가 실행됐는지 기록한다."""

    def __init__(self, monkeypatch, tmp_path, stages, argv=(), fail=(), gate_ok=True):
        self.ran, self.fail, self.gate_calls = [], set(fail), []
        monkeypatch.setattr(daily, 'STAGES', stages)
        monkeypatch.setattr(daily, 'LOG_DIR', tmp_path)
        monkeypatch.setattr(daily, '_acquire_lock', lambda: None)
        monkeypatch.setattr(daily, '_release_lock', lambda: None)
        monkeypatch.setattr(daily.sys, 'argv', ['daily', *argv])
        monkeypatch.setattr(daily.subprocess, 'run', lambda *a, **k: None)  # llm_usage_report

        def fake_stage(module, env, log, retry_limit=None, extra_args=(), log_lock=None, tag=''):
            self.ran.append(module)
            return (1 if module in self.fail else 0), ['stderr-tail'], 1.0, 0

        def fake_gate(printer=print, **kw):
            self.gate_calls.append(1)
            printer('  (fake gate)')
            return gate_ok, '테스트' if gate_ok else '무인 실행'

        monkeypatch.setattr(daily, '_run_stage_with_stall_retry', fake_stage)
        monkeypatch.setattr(daily.uc_gate, 'ensure_trust', fake_gate)

    def run(self):
        try:
            daily.main()
            return 0
        except SystemExit as e:
            return e.code or 0


_STAGES = [
    {'id': 'first', 'env': {}},
    {'id': 'second', 'env': {}},
    {'id': 'soft', 'env': {}, 'critical': False},
    {'id': 'uc_gate', 'kind': 'gate', 'critical': False},
    {'id': 'ucwork', 'module': 'pkg.ucwork', 'env': {}, 'critical': False, 'needs_uc': True},
    {'id': 'last', 'env': {}, 'critical': False},
]


class TestMainFlow:
    def test_happy_path_runs_everything_in_order(self, monkeypatch, tmp_path, capsys):
        h = _Harness(monkeypatch, tmp_path, _STAGES)
        assert h.run() == 0
        assert h.ran == ['first', 'second', 'soft', 'pkg.ucwork', 'last']
        assert len(h.gate_calls) == 1

    def test_critical_failure_stops_and_suggests_from(self, monkeypatch, tmp_path, capsys):
        h = _Harness(monkeypatch, tmp_path, _STAGES, fail=['second'])
        assert h.run() == 1
        assert h.ran == ['first', 'second']          # 뒤 단계는 안 돈다
        err = capsys.readouterr().err
        assert '--from second' in err

    def test_noncritical_failure_keeps_going(self, monkeypatch, tmp_path, capsys):
        h = _Harness(monkeypatch, tmp_path, _STAGES, fail=['soft'])
        assert h.run() == 0
        assert h.ran == ['first', 'second', 'soft', 'pkg.ucwork', 'last']
        out = capsys.readouterr()
        assert '--from soft' not in out.err        # 재개가 아니라 --only 안내여야 한다
        assert '--only soft' in out.err
        assert '실패한 보강 단계: soft' in out.out

    def test_gate_failure_skips_only_uc_stages(self, monkeypatch, tmp_path, capsys):
        h = _Harness(monkeypatch, tmp_path, _STAGES, gate_ok=False)
        assert h.run() == 0
        assert 'pkg.ucwork' not in h.ran           # uc 단계만 빠지고
        assert 'last' in h.ran                     # 나머지는 그대로 돈다
        assert '건너뜀' in capsys.readouterr().out

    def test_from_skips_the_head(self, monkeypatch, tmp_path, capsys):
        h = _Harness(monkeypatch, tmp_path, _STAGES, argv=['--from', 'soft'])
        assert h.run() == 0
        assert h.ran == ['soft', 'pkg.ucwork', 'last']

    def test_only_uc_stage_still_runs_gate_absent(self, monkeypatch, tmp_path, capsys):
        # --only로 uc 단계만 고르면 게이트가 목록에 없다 — 그래도 돌아야 한다(사람이 이미
        # 워밍업을 해뒀다고 보고 손으로 고른 것). uc_ok 초깃값이 True인 이유.
        h = _Harness(monkeypatch, tmp_path, _STAGES, argv=['--only', 'ucwork'])
        assert h.run() == 0
        assert h.ran == ['pkg.ucwork']
        assert h.gate_calls == []
