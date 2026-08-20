"""2026-08-20 리포 정리 — 45개 평평한 톱레벨 모듈을 pipeline/category/infra/tools 하위 패키지로
나누면서, 옛 경로(`gonggu.<이름>`)에 남긴 호환 shim이 실제로 옛 경로를 쓰는 모든 소비자
(daily.py의 서브프로세스 호출, 테스트의 `from gonggu.X import Y`, pyproject.toml의
`gonggu.X:main` 진입점)를 그대로 지원하는지 못박는다.

shim은 `sys.modules[__name__] = _real`로 자기 자신을 실제 모듈로 바꿔치기한다 — `from gonggu.X
import *`가 아니라 이 방식을 쓴 이유: `import *`는 밑줄로 시작하는 이름을 안 옮기는데,
`test_common_io.py`가 `from gonggu.common import _date_key_from_raw`를 쓰고 있어서 실제로 깨지는
걸 확인했다(2026-08-20). 이 파일은 그 회귀를 막는다.
"""
import importlib
import pathlib
import re
import runpy
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent

# (옛 톱레벨 이름, 옮긴 하위 패키지) — daily.py의 STAGES가 이 이름들을 그대로 쓰고,
# pyproject.toml의 [project.scripts]도 이 경로를 그대로 가리킨다.
MOVED = {
    'pipeline': ['update_gonggu_stage', 'fetch_source', 'fetch_yt_ppl', 'classify',
                 'classify_yt_ppl', 'transform', 'load', 'rescan_inprogress',
                 'backfill_period', 'sync_hifen_emails', 'maintenance', 'uc_gate',
                 'uc_healthcheck'],
    'category': ['build_category_dataset', 'classify_category', 'category_dashboard',
                 'export_unclassified'],
    'infra': ['common', 'crawl_pool', 'platforms', 'prompts', 'llm_batch', 'uc_engine'],
    'tools': ['unresolved_board', 'llm_usage_report', 'purge_marketplace_links',
              'crawl_linkbio', 'login_naver', 'case_matrix', 'check_db'],
}
ALL_MOVED = [(pkg, name) for pkg, names in MOVED.items() for name in names]


class TestShimIdentity:
    """옛 경로로 import하면 새 경로의 그 모듈 객체가 그대로 나와야 한다 — 복사본이 아니라
    동일 객체여야 monkeypatch(daily.uc_gate, 'ensure_trust', ...) 같은 패턴이 계속 통한다."""

    @pytest.mark.parametrize('pkg,name', ALL_MOVED)
    def test_old_path_is_identical_object_to_new_path(self, pkg, name):
        old = importlib.import_module(f'gonggu.{name}')
        new = importlib.import_module(f'gonggu.{pkg}.{name}')
        assert old is new
        assert sys.modules[f'gonggu.{name}'] is sys.modules[f'gonggu.{pkg}.{name}']

    def test_underscore_names_survive_the_shim(self):
        # import * 로 만든 shim이었다면 여기서 깨졌을 것 — 2026-08-20 실제로 발견된 회귀.
        from gonggu.common import _date_key_from_raw
        assert callable(_date_key_from_raw)

    def test_from_package_import_submodule_binds_the_real_module(self):
        # daily.py가 쓰는 정확한 형태: `from gonggu import uc_gate`.
        import gonggu
        from gonggu import uc_gate
        assert uc_gate is sys.modules['gonggu.pipeline.uc_gate']
        assert uc_gate is gonggu.uc_gate


class TestShimMechanismInIsolation:
    """실제 파이프라인 파일과 무관하게, shim 템플릿 자체(스왑 + __main__ 분기)가 옳은지
    합성 모듈 쌍으로 검증한다 — 프로덕션 파일을 고쳐도 이 메커니즘 자체의 계약은 안 변한다."""

    def _write_pair(self, tmp_path):
        pkg = tmp_path / 'fakepkg'
        pkg.mkdir()
        (pkg / '__init__.py').write_text('', encoding='utf-8')
        sub = pkg / 'real'
        sub.mkdir()
        (sub / '__init__.py').write_text('', encoding='utf-8')
        (sub / 'mod.py').write_text(
            'CALLS = []\n\ndef main():\n    CALLS.append(1)\n', encoding='utf-8')
        shim = pkg / 'mod.py'
        shim.write_text(
            'import sys\n'
            'from fakepkg.real import mod as _real\n'
            "if __name__ != '__main__':\n"
            '    sys.modules[__name__] = _real\n'
            'else:\n'
            '    _real.main()\n',
            encoding='utf-8')
        sys.path.insert(0, str(tmp_path))
        return pkg

    def test_normal_import_swaps_sys_modules_without_calling_main(self, tmp_path, monkeypatch):
        self._write_pair(tmp_path)
        try:
            mod = importlib.import_module('fakepkg.mod')
            real = importlib.import_module('fakepkg.real.mod')
            assert mod is real
            assert real.CALLS == []          # 그냥 import했을 뿐이니 main()은 안 불림
        finally:
            sys.path.remove(str(tmp_path))
            for name in ('fakepkg', 'fakepkg.mod', 'fakepkg.real', 'fakepkg.real.mod'):
                sys.modules.pop(name, None)

    def test_dash_m_execution_calls_main_without_corrupting_dunder_main(self, tmp_path):
        pkg = self._write_pair(tmp_path)
        try:
            # `python -m fakepkg.mod`와 동치 — run_name='__main__'으로 shim의 소스를 직접 돌린다.
            runpy.run_path(str(pkg / 'mod.py'), run_name='__main__')
            real = importlib.import_module('fakepkg.real.mod')
            assert real.CALLS == [1]
            # 이 실행 자체의 __main__ 모듈(pytest 프로세스)이 훼손되지 않아야 한다.
            assert sys.modules['__main__'].__name__ != 'fakepkg.real.mod'
        finally:
            sys.path.remove(str(tmp_path))
            for name in ('fakepkg', 'fakepkg.real', 'fakepkg.real.mod'):
                sys.modules.pop(name, None)


class TestRunPipelineRemoved:
    """daily.py가 완전 상위호환이라 2026-08-20에 지운 옛 오케스트레이터 — 되살아나지 않게."""

    def test_module_file_gone(self):
        assert not (ROOT / 'gonggu' / 'run_pipeline.py').exists()

    def test_no_entry_point_left(self):
        text = (ROOT / 'pyproject.toml').read_text(encoding='utf-8')
        assert 'run_pipeline' not in text


def _pyproject_entry_points():
    """pyproject.toml [project.scripts]의 `이름 = "module:attr"` 줄들을 뽑는다 — 정식 TOML
    파서 의존성을 새로 늘리지 않으려고 이 한 섹션(단순 flat key=value)만 정규식으로 읽는다."""
    text = (ROOT / 'pyproject.toml').read_text(encoding='utf-8')
    section = text.split('[project.scripts]')[1].split('\n[')[0]
    return re.findall(r'^\s*[\w-]+\s*=\s*"([\w.]+):(\w+)"', section, re.MULTILINE)


def test_pyproject_has_the_expected_number_of_entry_points():
    assert len(_pyproject_entry_points()) >= 15   # 2026-08-20 기준 18개 — 회귀 감지용 하한


@pytest.mark.parametrize('module,attr', _pyproject_entry_points())
def test_entry_point_resolves(module, attr):
    """shim이 깨지거나 오타가 나면 `pip install -e .` 사용자만 뒤늦게 발견하게 된다."""
    mod = importlib.import_module(module)
    assert callable(getattr(mod, attr))
