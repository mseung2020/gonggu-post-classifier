"""picker.finalize_pick의 medium/force_verify 재검증 경로가 core.resolve_product의 최초
LLM#3 판별과 같은 관대함으로 page_type을 처리하는지(2026-08-18 점검, 문제 13).

배경: 재검증(LLM#3 두 번째 호출)이 예전엔 "상품페이지 + 일치"가 아니면 무조건 unresolved로
끝냈다 — 최초 판별(core.py)이었다면 구제됐을 두 케이스(링크모음/스토어메인 재귀, 무관→hold
완화)를 재검증 경로에서만 놓치고 있었다. 지금은 두 경로가 같은 처리를 공유한다."""
import pytest

from gonggu.resolve_links import picker


class TestReverifyMatchesInitialJudgeLeniency:
    def test_reverify_finds_nested_collection_and_recurses_to_done(self, monkeypatch):
        """재검증 대상이 또 다른 링크모음/스토어메인이면, core.py처럼 한 홉 더 파고들어
        그 안에서 진짜 상품페이지를 찾으면 최종 확정한다."""
        monkeypatch.setattr(picker, 'pick_link',
                            lambda ctx, links: {'chosen_index': 0, 'confidence': 'medium', 'reason': 'ok'})
        monkeypatch.setattr(picker, 'fast_skip_uc_host', lambda url: False)
        monkeypatch.setattr(picker, 'fetch', lambda page, url, referer=None: {
            'error': None, 'via': 'browser', 'status': 200, 'final_url': url,
            'title': 't', 'jsonld': {}, 'og_image': None, 'body_text': 'x' * 300})
        judge_calls = {'n': 0}

        def fake_judge_page(ctx, page_info):
            judge_calls['n'] += 1
            if judge_calls['n'] == 1:
                return {'page_type': '링크모음', 'is_final_product_page': False, 'reason': '허브였음'}
            return {'page_type': '상품페이지', 'is_final_product_page': True, 'reason': '일치'}

        monkeypatch.setattr(picker, 'judge_page', fake_judge_page)
        monkeypatch.setattr(picker, 'extract_collection_links',
                            lambda page: [{'href': 'https://real-shop.example/product', 'text': '진짜상품'}])

        links = [{'href': 'https://hub.example.com/list', 'source': 'link'}]
        res = picker.finalize_pick(None, links, {'product_name': 'p'}, 'ctx', 'https://ref',
                                   '링크모음', prefetched_final=False)
        assert res['status'] == 'done'
        assert res['final_url'] == 'https://real-shop.example/product'
        assert judge_calls['n'] == 2  # 최초 판별 1회 + 재귀 후 재판별 1회

    def test_reverify_finds_unrelated_page_becomes_hold_not_unresolved(self, monkeypatch):
        """재검증 결과가 '무관'이면 core.py처럼 unresolved(자동 실패)가 아니라 hold(사람
        검토용)로 완화한다."""
        monkeypatch.setattr(picker, 'pick_link',
                            lambda ctx, links: {'chosen_index': 0, 'confidence': 'medium', 'reason': 'ok'})
        monkeypatch.setattr(picker, 'fast_skip_uc_host', lambda url: False)
        monkeypatch.setattr(picker, 'fetch', lambda page, url, referer=None: {
            'error': None, 'via': 'browser', 'status': 200, 'final_url': url,
            'title': 't', 'jsonld': {}, 'og_image': None, 'body_text': 'x'})
        monkeypatch.setattr(picker, 'judge_page', lambda ctx, page_info: {
            'page_type': '무관', 'is_final_product_page': False, 'reason': '전혀 다른 페이지'})

        links = [{'href': 'https://example.com/x', 'source': 'link'}]
        res = picker.finalize_pick(None, links, {'product_name': 'p'}, 'ctx', 'https://ref',
                                   '링크모음', prefetched_final=False)
        assert res['status'] == 'hold'
        assert '무관' in res['note']

    def test_reverify_collection_needing_dom_extraction_defers_to_tier1(self, monkeypatch):
        """재검증 fetch가 requests 패스트패스(via='http')로 끝났는데 그 페이지가 또 다른
        링크모음/스토어메인이면, DOM 추출 전에 브라우저로 다시 열어야 한다(core.py와 동일) —
        Tier0라 브라우저를 못 열면 DOM을 억지로 긁지 않고 needs_browser로 위임한다."""
        monkeypatch.setattr(picker, 'pick_link',
                            lambda ctx, links: {'chosen_index': 0, 'confidence': 'medium', 'reason': 'ok'})
        monkeypatch.setattr(picker, 'fast_skip_uc_host', lambda url: False)
        monkeypatch.setattr(picker, 'fetch', lambda page, url, referer=None: {
            'error': None, 'via': 'http', 'status': 200, 'final_url': url,
            'title': 't', 'jsonld': {}, 'og_image': None, 'body_text': 'x' * 300})
        monkeypatch.setattr(picker, 'judge_page', lambda ctx, page_info: {
            'page_type': '스토어메인', 'is_final_product_page': False})
        monkeypatch.setattr(picker, 'fetch_with_browser', lambda page, url, referer=None: {
            'error': None, 'via': 'needs_browser', 'status': None, 'final_url': None,
            'title': None, 'jsonld': {}, 'og_image': None, 'body_text': ''})
        monkeypatch.setattr(picker, 'extract_collection_links',
                            lambda page: pytest.fail('needs_browser인데 DOM 추출을 시도함'))

        links = [{'href': 'https://example.com/store', 'source': 'link'}]
        res = picker.finalize_pick(None, links, {'product_name': 'p'}, 'ctx', 'https://ref',
                                   '링크모음', prefetched_final=False)
        assert res['status'] == 'needs_browser'

    def test_reverify_collection_with_no_sub_links_is_unresolved(self, monkeypatch):
        monkeypatch.setattr(picker, 'pick_link',
                            lambda ctx, links: {'chosen_index': 0, 'confidence': 'medium', 'reason': 'ok'})
        monkeypatch.setattr(picker, 'fast_skip_uc_host', lambda url: False)
        monkeypatch.setattr(picker, 'fetch', lambda page, url, referer=None: {
            'error': None, 'via': 'browser', 'status': 200, 'final_url': url,
            'title': 't', 'jsonld': {}, 'og_image': None, 'body_text': 'x'})
        monkeypatch.setattr(picker, 'judge_page', lambda ctx, page_info: {
            'page_type': '링크모음', 'is_final_product_page': False})
        monkeypatch.setattr(picker, 'extract_collection_links', lambda page: [])

        links = [{'href': 'https://example.com/x', 'source': 'link'}]
        res = picker.finalize_pick(None, links, {'product_name': 'p'}, 'ctx', 'https://ref',
                                   '링크모음', prefetched_final=False)
        assert res['status'] == 'unresolved'
