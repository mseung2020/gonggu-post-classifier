"""uc 재검증 폴백의 순수 로직 검증 — 실제 uc/크롬/네트워크 없이 결정론 부분만 못박는다.
(uc가 네이버를 실제로 여는 통합 동작은 LIMIT 소량 실전 스모크로 확인하는 게 이 저장소 규약.)

검증 대상: (1) uc 폴백 게이트(_uc_enabled_for) — 환경변수/호스트 조건, (2) 차단 판정
(_looks_blocked), (3) uc html → rec 파서(rec_from_html)가 requests 경로와 같은 모양을 내는지,
(4) 2단 패스의 대상 SELECT가 unresolved+차단note만 고르는지.
"""
import pytest

from gonggu.resolve_links import browser
from gonggu.resolve_links.httpfetch import rec_from_html
from gonggu.resolve_links.reverify_uc import _select_sql
from gonggu.platforms import PLATFORMS


# ── uc 폴백 게이트 ───────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clear_uc_env(monkeypatch):
    monkeypatch.delenv('RESOLVE_UC', raising=False)
    monkeypatch.delenv('RESOLVE_UC_HOSTS', raising=False)


def test_uc_disabled_by_default():
    # RESOLVE_UC가 없으면 어떤 호스트든 폴백 안 함(대량 본 경로 무변경).
    assert browser._uc_enabled_for('https://smartstore.naver.com/x/products/1') is False


def test_uc_enabled_only_for_target_hosts(monkeypatch):
    monkeypatch.setenv('RESOLVE_UC', '1')
    assert browser._uc_enabled_for('https://smartstore.naver.com/x/products/1') is True
    assert browser._uc_enabled_for('https://brand.naver.com/y/products/2') is True
    # 기본 대상은 naver.뿐 — 자사몰은 폴백 대상 아님(Playwright로 충분)
    assert browser._uc_enabled_for('https://shop.cafe24.com/p/1') is False


def test_uc_hosts_configurable(monkeypatch):
    monkeypatch.setenv('RESOLVE_UC', '1')
    monkeypatch.setenv('RESOLVE_UC_HOSTS', 'naver.,gmarket.co.kr')
    assert browser._uc_enabled_for('https://item.gmarket.co.kr/x') is True
    assert browser._uc_enabled_for('https://smartstore.naver.com/x') is True
    assert browser._uc_enabled_for('https://www.auction.co.kr/x') is False


# ── 차단 판정 ────────────────────────────────────────────────────────────────

def test_blocked_by_login_redirect():
    assert browser._looks_blocked({'final_url': 'https://nid.naver.com/nidlogin.login?url=...'}) is True


def test_blocked_by_status():
    # 403/429 등 BLOCKED_STATUS_CODES
    from gonggu.resolve_links.config import BLOCKED_STATUS_CODES
    code = next(iter(BLOCKED_STATUS_CODES))
    assert browser._looks_blocked({'status': code, 'final_url': 'https://x', 'body_text': ''}) is True


def test_not_blocked_normal_page():
    assert browser._looks_blocked(
        {'status': 200, 'final_url': 'https://smartstore.naver.com/x/products/1',
         'body_text': '정가 59,000 공구가 41,300 무료배송'}) is False


# ── uc html → rec 파서 (requests 경로와 동일 모양) ───────────────────────────

def test_rec_from_html_shape_and_fields():
    html = '''<html><head>
      <meta property="og:title" content="베른호이체 미니피아노"/>
      <meta property="og:image" content="https://img.example.com/piano.jpg"/>
      <script type="application/ld+json">{"@type":"Product","name":"베른호이체 미니피아노",
        "offers":{"price":"41300"},"image":["https://img.example.com/piano.jpg"]}</script>
      </head><body>우리아이 첫 악기 미니피아노 41,300원 무료배송</body></html>'''
    rec = rec_from_html(html, 'https://smartstore.naver.com/main/products/123')
    assert rec['via'] == 'uc' and rec['status'] == 200 and rec['error'] is None
    assert rec['final_url'] == 'https://smartstore.naver.com/main/products/123'
    assert rec['title'] == '베른호이체 미니피아노'
    assert rec['og_image'] == 'https://img.example.com/piano.jpg'
    assert rec['jsonld'].get('name') == '베른호이체 미니피아노'
    assert '41,300' in rec['body_text']
    # browser.fetch()가 돌려주는 것과 같은 키 집합이어야 picker/core가 그대로 먹는다
    assert set(rec) == {'status', 'final_url', 'title', 'og_image', 'jsonld', 'body_text', 'error', 'via'}


def test_rec_from_html_empty_is_safe():
    rec = rec_from_html('', 'https://x')
    assert rec['title'] is None and rec['jsonld'] == {} and rec['body_text'] == ''


# ── 2단 대상 SELECT ──────────────────────────────────────────────────────────

def test_select_targets_unresolved_and_blocked_note():
    sql = _select_sql(PLATFORMS['ig'])
    assert "link_status = 'unresolved'" in sql
    assert 'link_note LIKE %s' in sql          # note 패턴은 파라미터로(% 이스케이프 안전)
    assert 'gonggu_post_product' in sql and 'gonggu_post' in sql
    # 재검증에 필요한 입력 컬럼이 다 실려야 한다
    for col in ('product_name', 'link_location', 'url_type', 'candidate_url', 'sort_order'):
        assert col in sql
