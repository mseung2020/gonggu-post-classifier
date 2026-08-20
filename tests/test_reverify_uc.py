"""uc 재검증 폴백의 순수 로직 검증 — 실제 uc/크롬/네트워크 없이 결정론 부분만 못박는다.
(uc가 네이버를 실제로 여는 통합 동작은 LIMIT 소량 실전 스모크로 확인하는 게 이 저장소 규약.)

검증 대상: (1) uc 폴백 게이트(_uc_enabled_for) — 환경변수/호스트 조건, (2) 차단 판정
(_looks_blocked), (3) uc html → rec 파서(rec_from_html)가 requests 경로와 같은 모양을 내는지,
(4) 2단 패스의 대상 SELECT가 unresolved+차단note만 고르는지.
"""
import pytest

from gonggu.resolve_links import browser
from gonggu.resolve_links import reverify_uc as ru
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
    # 확대(2026-08-19) — 두 종류의 차단 note를 OR로 받고, 파이썬 필터링에 쓸 link_note도 싣는다
    assert sql.count('link_note LIKE %s') == len(ru.BLOCKED_NOTE_LIKES) == 2
    assert 'pp.link_note' in sql


# ── 대상 확대 + 안전판 (2026-08-19) ──────────────────────────────────────────

class TestUcTargetSelection:
    """예전 쿼리('재검증 중 차단'만)는 실측 4건짜리 큐였고, 정작 uc가 뚫으라고 만들어진 네이버
    안티봇 건 988건은 '로그인월_차단'이라는 다른 문구로 쌓여 있었다. 확대하되, 사람이 곁에
    앉는 패스라 "열어봐야 소용없는 건"을 큐에 안 넣는 안전판 셋을 같이 검증한다."""

    @pytest.fixture(autouse=True)
    def _uc_on(self, monkeypatch):
        # _is_uc_addressable이 위임하는 browser._uc_enabled_for는 RESOLVE_UC=1일 때만 참
        monkeypatch.setenv('RESOLVE_UC', '1')
        monkeypatch.setenv('RESOLVE_UC_HOSTS', 'naver.,gmarket.co.kr,auction.co.kr,ohou.se,11st.co.kr')

    def test_dead_pages_are_excluded(self):
        for note in ('로그인월_차단 — SPAO 사이트의 404 오류 페이지로 상품 없음',
                     '로그인월_차단 — 존재하지 않는 프로필 오류 페이지',
                     '로그인월_차단 — 접근한 주소가 사라진 오류 페이지',
                     '로그인월_차단 — Instagram 프로필 접근 불가/로그인 필요',
                     '로그인월_차단 — 알리익스프레스 접근 차단 페이지로 상품 정보 없음'):
            assert ru._has_dead_marker(note) is True, note
            due, reason = ru.classify_target(note, 'https://smartstore.naver.com/x/products/1', None)
            assert due is False and '죽은 페이지' in reason

    def test_naver_antibot_is_addressable(self):
        note = '로그인월_차단 — 네이버 보안 확인 페이지로 접근 차단됨'
        assert ru.classify_target(note, 'https://m.smartstore.naver.com/a/products/1', None) == (True, '대상')

    def test_hub_url_admitted_via_antibot_note(self):
        """candidate_url이 인포크 허브라 호스트로는 못 걸러도, note가 안티봇을 가리키면 대상.
        resolve_product가 허브를 다시 걸어가며 최종 목적지에서 uc를 쓴다."""
        due, _ = ru.classify_target('로그인월_차단 — 네이버 보안 확인 페이지', 'https://inpk.link/abc', None)
        assert due is True

    def test_selfhosted_mall_without_antibot_note_is_skipped(self):
        """자사몰 로그인월은 Playwright로도 uc로도 같은 결과 — 사람 시간을 쓸 이유가 없다."""
        due, reason = ru.classify_target('로그인월_차단 — 로그인이 필요한 페이지',
                                         'https://hairwax.co.kr/product/1', None)
        assert due is False and 'uc 비대상' in reason

    def test_fast_skip_note_still_covered(self):
        """기존 대상('재검증 중 차단')이 확대 후에도 그대로 잡혀야 한다(회귀 방지)."""
        note = '재검증 중 차단(네이버/오픈마켓 로그인월 호스트) — fast에서 브라우저 생략, uc 패스 대상'
        assert ru.classify_target(note, 'https://smartstore.naver.com/x/products/1', None) == (True, '대상')

    def test_retires_after_max_attempts(self, monkeypatch):
        """uc로도 계속 못 뚫는 건은 은퇴 — 없으면 700건 규모에서 매 실행 사람 시간을 갉아먹는다."""
        monkeypatch.setattr(ru, 'UC_MAX_ATTEMPTS', 3)
        note = '로그인월_차단 — 네이버 보안 확인 페이지로 접근 차단됨'
        url = 'https://smartstore.naver.com/x/products/1'
        assert ru.classify_target(note, url, {'attempts': 2})[0] is True    # 아직 여유
        due, reason = ru.classify_target(note, url, {'attempts': 3})
        assert due is False and '은퇴' in reason
        assert ru.classify_target(note, url, None)[0] is True               # 이력 없으면 당연히 대상

    def test_retirement_beats_other_filters(self, monkeypatch):
        """은퇴 판정이 먼저 — 이미 상한을 채운 건은 다른 이유를 따질 것도 없다."""
        monkeypatch.setattr(ru, 'UC_MAX_ATTEMPTS', 1)
        due, reason = ru.classify_target('로그인월_차단 — 404 오류', 'https://x.kr', {'attempts': 5})
        assert due is False and '은퇴' in reason
