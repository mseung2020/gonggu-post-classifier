"""브라우저 없는 빠른 패스(Tier0)/브라우저 필요분만(Tier1) 분리(2026-08-18, 속도개선 공사
F단계) 계약.

배경: via_stats 실측(2026-08-18, daily_2026-08-18_135503.log)으로 후보 URL의 약 80%
(linkbio_structured 45% + uc_host_skip 9% + http 26%)가 브라우저를 아예 안 쓰고 끝나는데,
예전엔 이 80%도 브라우저 필요분(20%)까지 감안해 낮게 잡은 RESOLVE_CONCURRENCY 슬롯 수만큼만
병렬화됐다. 이제 browser.set_allow_browser(False) 상태에서는 브라우저가 필요한 순간
core.py/picker.py가 즉시 status='needs_browser'로 넘기고, runner._resolve_pending이 그 후보
(상품)만 모아 브라우저 허용 상태로 재시도한다.

실제 크롬/스레드/LLM 없이 각 계층(브라우저 게이팅 → core의 후보 집계 → picker의 재검증
분기 → runner의 2단계 오케스트레이션)을 독립적으로 검증한다."""
import threading
from types import SimpleNamespace

import pytest

from gonggu.resolve_links import browser, core, picker, runner


@pytest.fixture(autouse=True)
def _reset_allow_browser():
    """각 테스트가 끝나면 전역 스위치를 기본값(True)으로 되돌린다 — 순서에 따라 다른
    테스트에 새는 것을 막는다."""
    yield
    browser.set_allow_browser(True)


class TestBrowserGating:
    """set_allow_browser(False)면 fetch()/fetch_with_browser()가 실제로 브라우저를 열지 않고
    via='needs_browser'인 sentinel rec를 돌려준다."""

    def test_fetch_returns_needs_browser_when_http_fastpath_misses(self, monkeypatch):
        monkeypatch.setattr(browser, 'try_http_fetch', lambda url, referer: None)
        monkeypatch.setattr(browser, '_browser_fetch',
                            lambda *a, **k: pytest.fail('브라우저 막았는데 _browser_fetch가 호출됨'))
        browser.set_allow_browser(False)
        rec = browser.fetch(None, 'https://example.com/x')
        assert rec['via'] == 'needs_browser'
        assert rec['error'] is None and rec['status'] is None

    def test_fetch_falls_back_to_browser_when_allowed(self, monkeypatch):
        """기본(allow_browser=True)이면 예전처럼 http 미스 시 브라우저로 정상 폴백한다 — 게이팅
        코드가 본 경로(무변경 원칙)를 건드리지 않았는지 확인."""
        monkeypatch.setattr(browser, 'try_http_fetch', lambda url, referer: None)
        called = {}

        def _fake_browser_fetch(page, url, wait_extra, referer):
            called['hit'] = True
            return {'status': 200, 'final_url': url, 'title': 't', 'og_image': None,
                    'jsonld': {}, 'body_text': 'x', 'error': None, 'via': 'browser'}

        monkeypatch.setattr(browser, '_browser_fetch', _fake_browser_fetch)
        rec = browser.fetch(None, 'https://example.com/x')
        assert called.get('hit') is True
        assert rec['via'] == 'browser'

    def test_fetch_with_browser_gated_too(self, monkeypatch):
        monkeypatch.setattr(browser, '_browser_fetch',
                            lambda *a, **k: pytest.fail('브라우저 막았는데 _browser_fetch가 호출됨'))
        browser.set_allow_browser(False)
        rec = browser.fetch_with_browser(None, 'https://example.com/x')
        assert rec['via'] == 'needs_browser'

    def test_fetch_with_browser_still_works_when_allowed(self, monkeypatch):
        monkeypatch.setattr(browser, '_browser_fetch',
                            lambda *a, **k: {'status': 200, 'final_url': 'https://example.com/x',
                                             'title': 't', 'og_image': None, 'jsonld': {},
                                             'body_text': 'x', 'error': None, 'via': 'browser'})
        rec = browser.fetch_with_browser(None, 'https://example.com/x')
        assert rec['via'] == 'browser'


class TestCoreNeedsBrowser:
    """core.py가 needs_browser를 LLM 호출 전에 끊고(빈 근거로 잘못 판정하지 않게), 상품
    전체를 언제 Tier1로 미룰지 정확히 결정하는지."""

    def test_resolve_one_candidate_short_circuits_before_llm(self, monkeypatch):
        monkeypatch.setattr(core, 'linkbio_candidates', lambda url: None)
        monkeypatch.setattr(core, 'fast_skip_uc_host', lambda url: False)
        monkeypatch.setattr(core, 'fetch', lambda *a, **k: {
            'error': None, 'via': 'needs_browser', 'status': None, 'final_url': None,
            'title': None, 'jsonld': {}, 'og_image': None, 'body_text': ''})
        monkeypatch.setattr(core, 'judge_page',
                            lambda *a, **k: pytest.fail('needs_browser인데 LLM#3(judge_page)이 호출됨'))
        res = core._resolve_one_candidate(None, 'https://shop.example.com/p',
                                          {'product_name': 'p'}, '')
        assert res['status'] == 'needs_browser'

    def test_resolve_product_defers_whole_item_when_no_done_and_some_needs_browser(self, monkeypatch):
        """후보 2개 중 1개는 needs_browser, 나머지 1개는 진짜 unresolved — done이 하나도
        없으면 unresolved로 확정하지 말고 상품 전체를 Tier1로 미뤄야 한다(그 needs_browser
        후보가 실은 done이었을 수 있으므로)."""
        calls = []

        def fake_resolve_one(page, url, product, ctx):
            calls.append(url)
            if 'a.com' in url:
                return {'status': 'needs_browser', 'final_url': None, 'note': 'x'}
            return {'status': 'unresolved', 'final_url': None, 'note': 'y'}

        monkeypatch.setattr(core, '_resolve_one_candidate', fake_resolve_one)
        monkeypatch.setattr(core, 'rank_candidates', lambda urls, handle: urls)
        product = {'candidate_url': 'https://a.com/1;https://b.com/2', 'product_name': 'p'}
        res = core.resolve_product(None, 'ig', {'user_id': 'u'}, product)
        assert res['status'] == 'needs_browser'
        assert len(calls) == 2  # 둘 다 실제로 시도됨(뒤 후보가 done일 가능성을 안 버림)

    def test_resolve_product_prefers_done_over_needs_browser(self, monkeypatch):
        """다른 후보가 done이면, 먼저 만난 needs_browser 후보는 무시하고 그 done을 즉시 확정한다
        — 이 상품은 Tier1로 미룰 필요조차 없다."""
        def fake_resolve_one(page, url, product, ctx):
            if 'a.com' in url:
                return {'status': 'needs_browser', 'final_url': None, 'note': 'x'}
            return {'status': 'done', 'final_url': url, 'note': 'ok'}

        monkeypatch.setattr(core, '_resolve_one_candidate', fake_resolve_one)
        monkeypatch.setattr(core, 'rank_candidates', lambda urls, handle: urls)
        product = {'candidate_url': 'https://a.com/1;https://b.com/2', 'product_name': 'p'}
        res = core.resolve_product(None, 'ig', {'user_id': 'u'}, product)
        assert res['status'] == 'done'

    def test_resolve_product_all_needs_browser_defers(self, monkeypatch):
        """best가 한 번도 안 채워진 채(전 후보가 needs_browser) 끝나도 죽지 않고 상품을
        정상적으로 Tier1로 미룬다."""
        monkeypatch.setattr(core, '_resolve_one_candidate',
                            lambda page, url, product, ctx: {'status': 'needs_browser',
                                                             'final_url': None, 'note': 'x'})
        monkeypatch.setattr(core, 'rank_candidates', lambda urls, handle: urls)
        product = {'candidate_url': 'https://a.com/1', 'product_name': 'p'}
        res = core.resolve_product(None, 'ig', {'user_id': 'u'}, product)
        assert res['status'] == 'needs_browser'

    def test_collection_branch_defers_when_dom_extraction_needs_browser(self, monkeypatch):
        """링크모음/스토어메인 판정 이후 DOM 추출을 위한 fetch_with_browser가 needs_browser를
        돌려주면(http 패스트패스로 초기 판정은 됐지만 렌더링은 못 함) 상품을 Tier1로 미룬다."""
        monkeypatch.setattr(core, 'linkbio_candidates', lambda url: None)
        monkeypatch.setattr(core, 'fast_skip_uc_host', lambda url: False)
        monkeypatch.setattr(core, 'fetch', lambda *a, **k: {
            'error': None, 'via': 'http', 'status': 200, 'final_url': 'https://shop.example.com/p',
            'title': 't', 'jsonld': {}, 'og_image': None, 'body_text': 'x' * 300})
        monkeypatch.setattr(core, 'judge_page',
                            lambda *a, **k: {'page_type': '링크모음', 'is_final_product_page': False})
        monkeypatch.setattr(core, 'fetch_with_browser', lambda *a, **k: {
            'error': None, 'via': 'needs_browser', 'status': None, 'final_url': None,
            'title': None, 'jsonld': {}, 'og_image': None, 'body_text': ''})
        res = core._resolve_one_candidate(None, 'https://shop.example.com/p',
                                          {'product_name': 'p'}, '')
        assert res['status'] == 'needs_browser'


class TestPickerNeedsBrowser:
    """picker.finalize_pick의 medium/force_verify 재검증 fetch가 needs_browser면 unresolved로
    확정하지 않고 needs_browser를 그대로 위로 흘려보낸다."""

    def test_medium_confidence_defers_when_reverify_needs_browser(self, monkeypatch):
        monkeypatch.setattr(picker, 'pick_link',
                            lambda ctx, links: {'chosen_index': 0, 'confidence': 'medium', 'reason': 'ok'})
        monkeypatch.setattr(picker, 'fast_skip_uc_host', lambda url: False)
        monkeypatch.setattr(picker, 'fetch', lambda *a, **k: {
            'error': None, 'via': 'needs_browser', 'status': None, 'final_url': None,
            'title': None, 'jsonld': {}, 'og_image': None, 'body_text': ''})
        monkeypatch.setattr(picker, 'judge_page',
                            lambda *a, **k: pytest.fail('needs_browser인데 LLM#3(judge_page)이 호출됨'))
        links = [{'href': 'https://shop.example.com/p', 'source': 'product'}]
        res = picker.finalize_pick(None, links, {'product_name': 'p'}, '', 'https://ref',
                                   '링크모음', prefetched_final=False)
        assert res['status'] == 'needs_browser'


class TestResolvePendingTwoPhase:
    """runner._resolve_pending — Tier0(브라우저 없는 빠른 패스)에서 안 끝난 것만 Tier1로
    넘기는 오케스트레이션. 실제 crawl_pool/Playwright 없이 run_crawl_pool을 가벼운 순차 페이크로
    바꿔 검증한다(daily.py의 샤딩 테스트와 같은 방식)."""

    def _fake_run_crawl_pool(self, items, handle, **kwargs):
        lock = threading.Lock()
        for item in items:
            ctx = SimpleNamespace(page=None, worker_id=0, lock=lock, state=None)
            handle(ctx, item)
        return kwargs.get('concurrency')

    def test_fast_pass_only_when_nothing_needs_browser(self, monkeypatch):
        monkeypatch.setattr(runner, 'run_crawl_pool', self._fake_run_crawl_pool)
        monkeypatch.setattr(runner, 'append_jsonl', lambda *a, **k: None)
        calls = []

        def fake_resolve(page, platform, parent, p):
            calls.append(p['sort_order'])
            return {'status': 'done', 'final_url': 'https://x', 'note': 'ok'}

        monkeypatch.setattr(runner, 'resolve_product', fake_resolve)
        pending = [(f'k{i}', {'platform': 'ig', 'parent': {}}, {'sort_order': i})
                   for i in range(3)]
        resolutions = {}
        allow_states = []
        orig_set = browser.set_allow_browser

        def _track(v):
            allow_states.append(v)
            orig_set(v)
        monkeypatch.setattr(runner, 'set_allow_browser', _track)

        runner._resolve_pending(pending, resolutions, total=3)
        assert len(resolutions) == 3
        assert all(r['status'] == 'done' for r in resolutions.values())
        assert allow_states == [False, True]  # Tier0 진입 시 끄고, 끝나면 다시 켬(Tier1 없음)

    def test_needs_browser_items_retried_in_second_phase_with_browser_allowed(self, monkeypatch):
        """Tier0에서 needs_browser로 보류된 항목만 Tier1로 넘어가고, 거기선 실제로 done이
        나올 수 있다(브라우저 허용 상태에서 resolve_product를 다시 부르므로)."""
        monkeypatch.setattr(runner, 'run_crawl_pool', self._fake_run_crawl_pool)
        monkeypatch.setattr(runner, 'append_jsonl', lambda *a, **k: None)

        def fake_resolve(page, platform, parent, p):
            # 브라우저가 아직 안 열려있으면(Tier0) needs_browser, 열려있으면(Tier1) done —
            # browser.browser_allowed()로 지금이 어느 패스인지 판단(실제 core.py가 하는 것과
            # 같은 신호를 씀).
            if p['sort_order'] == 1 and not browser.browser_allowed():
                return {'status': 'needs_browser', 'final_url': None, 'note': 'defer'}
            return {'status': 'done', 'final_url': 'https://x', 'note': 'ok'}

        monkeypatch.setattr(runner, 'resolve_product', fake_resolve)
        pending = [(f'k{i}', {'platform': 'ig', 'parent': {}}, {'sort_order': i})
                   for i in range(3)]
        resolutions = {}
        runner._resolve_pending(pending, resolutions, total=3)
        assert len(resolutions) == 3
        assert all(r['status'] == 'done' for r in resolutions.values())  # 결국 전부 확정됨
        assert browser.browser_allowed() is True  # Tier1 끝난 뒤 상태 정상 복구

    def test_needs_browser_row_survives_tier1_reappearing_as_needs_browser(self, monkeypatch):
        """Tier1(브라우저 허용 상태)에서도 needs_browser가 다시 나오면(있어선 안 되는 내부
        버그) 무한 보류 대신 error로 강등해 이번 실행을 끝낸다 — 다음 실행에서 pending에
        다시 잡혀 재시도된다(체크포인트 계약)."""
        monkeypatch.setattr(runner, 'run_crawl_pool', self._fake_run_crawl_pool)
        monkeypatch.setattr(runner, 'append_jsonl', lambda *a, **k: None)
        monkeypatch.setattr(runner, 'resolve_product',
                            lambda page, platform, parent, p: {'status': 'needs_browser',
                                                               'final_url': None, 'note': 'x'})
        pending = [('k0', {'platform': 'ig', 'parent': {}}, {'sort_order': 0})]
        resolutions = {}
        runner._resolve_pending(pending, resolutions, total=1)
        assert resolutions['k0']['status'] == 'error'
        assert '내부 로직 오류' in resolutions['k0']['note']
