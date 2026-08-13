"""리졸브 1/2단 분리(2026-08-13) — fast(무인)는 네이버/오픈마켓 로그인월 호스트를 브라우저로
열지 않고 '재검증 중 차단' 노트로 넘기고, 기존 reverify_uc(RESOLVE_UC=1)가 2단에서 uc로 실제로 연다.
실제 크롬/네트워크 없이 순수 함수 + fetch 호출 여부로 검증한다."""
import pytest

from gonggu.resolve_links import core, picker, reverify_uc
from gonggu.resolve_links.antibot import is_uc_host
from gonggu.resolve_links.browser import UC_SKIP_NOTE, fast_skip_uc_host


def test_is_uc_host():
    assert is_uc_host('https://smartstore.naver.com/x/products/1')
    assert is_uc_host('https://item.gmarket.co.kr/Item?goodscode=1')
    assert is_uc_host('https://store.ohou.se/products/1')
    assert is_uc_host('https://www.11st.co.kr/products/9')
    assert not is_uc_host('https://shop.cafe24.com/product/1')
    assert not is_uc_host('https://link.inpock.co.kr/abc')     # 링크바이오 허브는 스킵 대상 아님
    assert not is_uc_host('')


def test_fast_skip_gated_by_resolve_uc(monkeypatch):
    """fast(RESOLVE_UC 없음)면 uc 호스트를 스킵, reverify 패스(RESOLVE_UC=1)면 스킵 안 함(실제 uc로 엶)."""
    monkeypatch.delenv('RESOLVE_UC', raising=False)
    assert fast_skip_uc_host('https://smartstore.naver.com/x') is True
    assert fast_skip_uc_host('https://shop.cafe24.com/x') is False   # 자사몰은 fast에서 그대로 시도
    monkeypatch.setenv('RESOLVE_UC', '1')
    assert fast_skip_uc_host('https://smartstore.naver.com/x') is False


def test_uc_skip_note_matches_reverify_filter():
    """fast-skip 노트를 reverify_uc의 LIKE 필터가 반드시 잡아야 2단으로 넘어간다(정합 회귀 가드)."""
    core_marker = reverify_uc.BLOCKED_NOTE_LIKE.strip('%')       # '재검증 중 차단'
    assert core_marker and core_marker in UC_SKIP_NOTE


def test_core_direct_uc_host_skips_browser(monkeypatch):
    """네이버 직행 후보: fast에서 fetch(브라우저)를 호출하지 않고 즉시 unresolved+노트로 넘긴다."""
    monkeypatch.delenv('RESOLVE_UC', raising=False)
    monkeypatch.setattr(core, 'linkbio_candidates', lambda url: None)   # 네이버는 링크바이오 아님
    monkeypatch.setattr(core, 'fetch', lambda *a, **k: pytest.fail('fast-skip인데 fetch가 호출됨'))
    res = core._resolve_one_candidate(None, 'https://smartstore.naver.com/x/products/1',
                                      {'product_name': 'p'}, '')
    assert res['status'] == 'unresolved' and '재검증 중 차단' in res['note']


def test_core_non_uc_host_not_skipped(monkeypatch):
    """자사몰은 스킵 대상이 아니라 정상 경로(fetch)로 간다 — fetch가 실제로 호출됨을 확인."""
    monkeypatch.delenv('RESOLVE_UC', raising=False)
    monkeypatch.setattr(core, 'linkbio_candidates', lambda url: None)
    called = {}
    def _fake_fetch(page, url, **k):
        called['url'] = url
        return {'error': 'stop-here', 'status': None, 'final_url': None}
    monkeypatch.setattr(core, 'fetch', _fake_fetch)
    res = core._resolve_one_candidate(None, 'https://shop.cafe24.com/product/1',
                                      {'product_name': 'p'}, '')
    assert called.get('url') == 'https://shop.cafe24.com/product/1'   # 스킵 안 하고 열었다
    assert res['status'] == 'error'                                  # 위 가짜 fetch의 error 경로


def test_picker_force_verify_uc_host_skips_before_fetch(monkeypatch):
    """구조화 링크바이오의 bare 'link' 후보(force_verify)가 네이버를 가리키면, 재검증 fetch를
    호출하지 않고 unresolved+노트로 넘긴다(구조화 최종확정 경로 elif는 영향 없음)."""
    monkeypatch.delenv('RESOLVE_UC', raising=False)
    monkeypatch.setattr(picker, 'pick_link',
                        lambda ctx, links: {'chosen_index': 0, 'confidence': 'high', 'reason': 'ok'})
    monkeypatch.setattr(picker, 'fetch', lambda *a, **k: pytest.fail('fast-skip인데 재검증 fetch가 호출됨'))
    links = [{'href': 'https://smartstore.naver.com/x/products/1', 'source': 'link'}]  # source=link → force_verify
    res = picker.finalize_pick(None, links, {'product_name': 'p'}, '', 'https://ref',
                               '링크인바이오(구조화)', prefetched_final=True)
    assert res['status'] == 'unresolved' and '재검증 중 차단' in res['note']
