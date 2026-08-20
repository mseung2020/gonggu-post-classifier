"""알려진 링크모음 호스트 지름길(2026-08-19).

배경: 링크모음 판별은 두 층이다. 1층은 도메인 대조(공짜), 2층은 페이지를 열어 LLM#3에게
"이 페이지 뭐야?"를 묻는 것(비쌈). 몇 주 돌린 이력을 캐보니 LLM#3가 "링크모음"이라 판정했는데
1층 목록엔 없는 호스트가 129종·752회였다 — 매번 답을 아는 질문에 돈을 낸 셈이다.

config.KNOWN_HUB_HOSTS는 "링크모음인 건 확실한데 구조화 파서는 없는" 곳이다. 브라우저는 여전히
필요하지만(DOM에서 <a>를 긁어야 하니) LLM#3 홉과 중복 페치는 건너뛴다.

실제 크롬/네트워크/LLM 없이, 호출 여부와 반환 모양으로만 검증한다.
"""
import pytest

from gonggu.resolve_links import core
from gonggu.resolve_links.antibot import is_known_hub


class TestIsKnownHub:
    def test_matches_configured_services(self):
        assert is_known_hub('https://linkbio.co/sunny-market')
        assert is_known_hub('https://my.wiredy.io/someone')

    def test_matches_per_account_subdomains(self):
        """계정마다 서브도메인이 다른 서비스 — 실측에서 linkstory.co.kr 15종, tuk.link 6종.
        완전 일치였다면 목록에 서비스를 넣어도 전부 샜다."""
        assert is_known_hub('https://jiy1067.linkstory.co.kr/')
        assert is_known_hub('https://dakkongbebe.linkstory.co.kr/')
        assert is_known_hub('https://monansalim.tuk.link/')

    def test_does_not_match_others(self):
        assert not is_known_hub('https://smartstore.naver.com/x/products/1')
        assert not is_known_hub('https://link.inpock.co.kr/abc')   # 파서가 있는 쪽(더 싼 경로)
        assert not is_known_hub('')

    def test_suffix_matching_does_not_overreach(self):
        """접미사 매칭이 남의 도메인을 삼키면 안 된다."""
        assert not is_known_hub('https://nottuk.link/x')
        assert not is_known_hub('https://evil-linkbio.co/x')


class _Recorder:
    """core가 무엇을 불렀는지만 기록하는 스텁."""

    def __init__(self):
        self.calls = []


@pytest.fixture
def stub(monkeypatch):
    rec = _Recorder()

    def _no_linkbio(url):
        return None    # 구조화 파서 대상 아님(그 경로는 별도 테스트가 덮는다)

    def _boom_fetch(*a, **k):
        rec.calls.append('fetch')
        raise AssertionError('알려진 허브인데 일반 fetch 경로로 샜다')

    def _boom_judge(*a, **k):
        rec.calls.append('judge_page')
        raise AssertionError('알려진 허브인데 LLM#3(페이지 종류 판별)을 불렀다')

    monkeypatch.setattr(core, 'linkbio_candidates', _no_linkbio)
    monkeypatch.setattr(core, 'fast_skip_uc_host', lambda u: False)
    monkeypatch.setattr(core, 'fetch', _boom_fetch)
    monkeypatch.setattr(core, 'judge_page', _boom_judge)
    return rec


class TestKnownHubShortcut:
    def test_skips_llm3_and_goes_straight_to_dom_extraction(self, stub, monkeypatch):
        """핵심 계약 — LLM#3도 일반 fetch도 안 부르고, 바로 브라우저 + DOM 추출 + LLM#2로 간다."""
        monkeypatch.setattr(core, 'fetch_with_browser',
                            lambda p, u: {'error': None, 'via': 'browser', 'final_url': u})
        monkeypatch.setattr(core, 'extract_collection_links',
                            lambda p: [{'href': 'https://mall.example/p/1', 'text': '스텐 냄비'}])
        monkeypatch.setattr(core, 'finalize_pick',
                            lambda *a, **k: {'status': 'done', 'final_url': 'https://mall.example/p/1',
                                             'note': 'ok', '_label': a[5]})
        res = core._resolve_one_candidate(None, 'https://jiy1067.linkstory.co.kr/',
                                          {'product_name': '스텐 냄비'}, '')
        assert res['status'] == 'done'
        assert res['_label'] == '링크모음'    # finalize_pick에 넘긴 페이지 종류 라벨
        assert stub.calls == []               # fetch/judge_page 둘 다 안 불림

    def test_defers_to_tier1_when_no_browser(self, stub, monkeypatch):
        """Tier0(브라우저 없는 빠른 패스)에서는 확정하지 않고 Tier1으로 미룬다 — 여기서
        unresolved로 굳히면 실제로는 풀렸을 건을 브라우저가 없었다는 이유로 버리게 된다."""
        monkeypatch.setattr(core, 'fetch_with_browser',
                            lambda p, u: {'error': None, 'via': 'needs_browser', 'final_url': None})
        res = core._resolve_one_candidate(None, 'https://monansalim.tuk.link/',
                                          {'product_name': 'x'}, '')
        assert res['status'] == 'needs_browser'

    def test_extraction_failure_is_unresolved_not_crash(self, stub, monkeypatch):
        monkeypatch.setattr(core, 'fetch_with_browser',
                            lambda p, u: {'error': None, 'via': 'browser', 'final_url': u})
        monkeypatch.setattr(core, 'extract_collection_links', lambda p: [])
        res = core._resolve_one_candidate(None, 'https://linkbio.co/x', {'product_name': 'x'}, '')
        assert res['status'] == 'unresolved' and '추출 실패' in res['note']

    def test_fetch_error_is_reported(self, stub, monkeypatch):
        monkeypatch.setattr(core, 'fetch_with_browser',
                            lambda p, u: {'error': 'timeout', 'via': 'browser', 'final_url': None})
        res = core._resolve_one_candidate(None, 'https://linkbio.co/x', {'product_name': 'x'}, '')
        assert res['status'] == 'error' and res['note'] == 'timeout'


class TestPermitInstrumentation:
    """허가증 점유 계측(2026-08-19) — 브라우저 허가증을 쥔 채 LLM을 기다리는 시간이 얼마나
    되는지 재는 눈금. "크롤 단계와 LLM 단계를 분리"하는 구조 변경이 값을 하는지를 감이 아니라
    이 숫자로 판단하려고 넣었다(단순히 LLM 직전에 허가증을 놓는 처방은 재기동 3.9초 때문에
    오히려 느리다는 실측이 이미 있다)."""

    @pytest.fixture(autouse=True)
    def _reset(self):
        from gonggu.resolve_links import browser as b

        def clear():
            with b._permit_lock:
                b._permit_stats.update(held_sec=0.0, busy_sec=0.0, sessions=0)
                b._permit_open.clear()

        clear()
        yield
        clear()

    def test_empty_sample_is_safe(self):
        from gonggu.resolve_links.browser import permit_stats
        assert permit_stats()['idle_ratio'] is None      # 0으로 나누지 않는다

    def test_open_session_counts_toward_held(self):
        """⚠ 첫 구현의 버그 — held는 브라우저를 닫을 때만(_teardown) 쌓는데 busy는 살아있는
        워커 것까지 다 세서, 분모만 빠진 채 "총 449초 중 작업 1202초, 유휴 -168%"라는 음수가
        실제로 찍혔다(2026-08-20). 아직 안 닫힌 세션의 경과 시간도 held에 들어가야 한다."""
        import time
        from gonggu.resolve_links import browser as b
        owner = object()
        b._permit_acquired(owner)
        b._add_permit_busy(0.01)
        time.sleep(0.05)
        s = b.permit_stats()
        assert s['open_sessions'] == 1
        assert s['held_sec'] >= 0.05
        assert 0 < s['idle_ratio'] < 1, f'유휴율이 0~1 밖: {s}'

    def test_closing_does_not_double_count(self):
        from gonggu.resolve_links import browser as b
        owner = object()
        b._permit_acquired(owner)
        before = b.permit_stats()['held_sec']
        b._permit_released(owner)
        after = b.permit_stats()
        assert after['open_sessions'] == 0 and after['sessions'] == 1
        assert after['held_sec'] >= before        # 줄지 않고, 이중 계산도 없음
        assert after['held_sec'] < before + 0.5

    def test_diagnostic_prints_nothing_without_samples(self, capsys):
        from gonggu.resolve_links.browser import permit_stats
        from gonggu.resolve_links.runner import _print_permit_diagnostics
        _print_permit_diagnostics(permit_stats())
        assert capsys.readouterr().out == ''


class TestDiagnosticVerdicts:
    """진단(_diag_unknown_hubs)이 위험한 조언을 하지 않는지 — 초판은 naver.com을 '허브 추가
    후보 1순위'로 추천했다(실제로는 LLM#3가 cafe.naver.com 75회를 링크모음으로 오분류한 것)."""

    def test_flags_llm_mislabels_as_not_a_hub(self):
        from gonggu._diag_unknown_hubs import is_definitely_not_a_hub
        for host in ('cafe.naver.com', 'smartstore.naver.com', 'open.kakao.com',
                     'pf.kakao.com', 'm.blog.naver.com'):
            assert is_definitely_not_a_hub(host) is True, host

    def test_does_not_flag_real_hubs(self):
        from gonggu._diag_unknown_hubs import is_definitely_not_a_hub
        for host in ('page.im', 'linkbio.co', 'jiy1067.linkstory.co.kr'):
            assert is_definitely_not_a_hub(host) is False, host

    def test_registrable_handles_two_level_cctld(self):
        from gonggu._diag_unknown_hubs import registrable
        assert registrable('jiy1067.linkstory.co.kr') == 'linkstory.co.kr'
        assert registrable('monansalim.tuk.link') == 'tuk.link'
        assert registrable('page.im') == 'page.im'
