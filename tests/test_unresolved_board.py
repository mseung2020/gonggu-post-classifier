"""unresolved_board(진행중 미해결 진단 보드)의 DB 없이 검증 가능한 부분 —
SQL 조립·행 가공·HTML 렌더.

진단 전용 모듈이라 파이프라인 판정에는 영향이 없지만, 세 가지는 못박아둔다.
  1) 읽기 전용: 생성되는 SQL에 쓰기 구문이 절대 없다.
  2) 상품 단위 필터: stage/link_status 조건이 부모(p.)가 아니라 상품(pp.)에서 걸린다
     (기간→상품 이전 대공사의 핵심 — 여기서 부모를 보면 예고 달력에서 오답이 난다).
  3) 자기완결 HTML: 외부 요청 0개, 캡션에 </script>가 들어와도 안전.
"""
import datetime
import re

import pytest

from gonggu import unresolved_board as ub


# ------------------------------------------------------------------ SQL
def _cols(sql):
    """최상위 콤마만 세서 SELECT 컬럼 수를 구한다(스칼라 서브쿼리 안의 콤마는 무시)."""
    head, depth, out = sql.split('\nFROM ')[0], 0, []
    for ch in head:
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
        elif depth == 0:
            out.append(ch)
    return ''.join(out).count(',') + 1


def test_두_플랫폼_SELECT의_컬럼수와_별칭이_같다():
    ig, yt = ub._select_sql('ig'), ub._select_sql('yt')
    assert _cols(ig) == _cols(yt)
    for alias in ('platform', 'product_id', 'native_id', 'owner_id', 'source_url',
                  'parent_title', 'external_url', 'publish_dt', 'sd', 'ed', 'stage',
                  'sib_n', 'sib_done_n'):
        assert f'AS {alias}' in ig and f'AS {alias}' in yt, alias


def test_플랫폼별_테이블과_날짜컬럼이_메타에서_온다():
    ig, yt = ub._select_sql('ig'), ub._select_sql('yt')
    assert 'FROM gonggu_post_product pp' in ig and 'JOIN gonggu_post p' in ig
    assert 'p.publish_date AS publish_dt' in ig
    assert 'FROM gonggu_video_product pp' in yt and 'JOIN gonggu_video p' in yt
    assert 'p.publishDate AS publish_dt' in yt
    assert 'p.video_url AS source_url' in yt and 'p.url AS source_url' in ig


def test_필터는_상품행_기준이다():
    """기간/스테이지·링크상태 모두 상품(pp) 컬럼으로 걸려야 한다."""
    sql = ub._select_sql('ig')
    assert "pp.gonggu_stage IN ('진행중')" in sql
    assert "pp.link_status IN ('unresolved', 'hold')" in sql
    # 부모 별칭 p.(pp.가 아닌)로 기간/상태를 읽는 곳이 하나도 없어야 한다
    assert not re.search(r'(?<![a-z])p\.gonggu_', sql)
    assert not re.search(r'(?<![a-z])p\.link_status', sql)


def test_status_조건_NULL토큰():
    assert ub._status_pred(('unresolved',)) == "(pp.link_status IN ('unresolved'))"
    p = ub._status_pred(('unresolved', ub.NULL_TOKEN))
    assert p == "(pp.link_status IN ('unresolved') OR pp.link_status IS NULL)"
    assert ub._status_pred((ub.NULL_TOKEN,)) == '(pp.link_status IS NULL)'
    with pytest.raises(ValueError):
        ub._status_pred(())
    with pytest.raises(ValueError):
        ub._stage_pred(())


def test_읽기전용_SQL만_만든다():
    for code in ('ig', 'yt'):
        sql = ub._select_sql(code, ('unresolved', 'hold', ub.NULL_TOKEN),
                             ('진행중', '시작전')).upper()
        for bad in ('UPDATE ', 'INSERT ', 'DELETE ', 'DROP ', 'ALTER ', 'CREATE ', 'TRUNCATE'):
            assert bad not in sql, bad


# ------------------------------------------------------------------ 행 가공
def test_호스트_추출():
    assert ub.host_of('https://www.smartstore.naver.com/abc/products/1') == 'smartstore.naver.com'
    assert ub.host_of('//litt.ly/user?x=1') == 'litt.ly'
    assert ub.host_of('') == '' and ub.host_of(None) == ''
    assert ub.host_of('그냥텍스트') == ''


def test_dday와_버킷():
    t = datetime.date(2026, 8, 10)
    assert ub.dday_of(datetime.date(2026, 8, 13), t) == 3
    assert ub.dday_of(datetime.datetime(2026, 8, 9, 12), t) == -1
    assert ub.dday_of(None, t) is None
    assert ub.dday_bucket(None) == '종료일미상'
    assert ub.dday_bucket(-1) == '이미지남(stage확인)'
    assert ub.dday_bucket(0) == '오늘마감'
    assert ub.dday_bucket(2) == 'D-2이내'
    assert ub.dday_bucket(7) == 'D-7이내'
    assert ub.dday_bucket(30) == 'D-8이상'


def test_재탐색_이력_라벨():
    assert ub.retry_info(None)['retry_state'] == '미시도'
    assert ub.retry_info({'attempts': 2, 'next_due': '2026-08-12'})['retry_state'] == '대기중'
    retired = ub.retry_info({'attempts': 5, 'next_due': None, 'checked_at': '2026-08-09'})
    assert retired['retry_state'] == '은퇴(소진)' and retired['retry_n'] == 5


def test_프로필_링크는_숫자_user_id면_만들지_않는다():
    assert ub.profile_url({'platform': 'ig', 'owner_id': 'mom_market'}) \
        == 'https://www.instagram.com/mom_market/'
    assert ub.profile_url({'platform': 'ig', 'owner_id': '17841400000'}) == ''
    assert ub.profile_url({'platform': 'yt', 'owner_id': 'UCabc'}) \
        == 'https://www.youtube.com/channel/UCabc'
    assert ub.profile_url({'platform': 'yt', 'owner_id': None}) == ''


def _db_row(**kw):
    base = dict(platform='ig', product_id=11, native_id='POST1', owner_id='mom_market',
                source_url='https://www.instagram.com/p/POST1/', parent_title='',
                external_url=None, publish_dt=datetime.datetime(2026, 8, 1, 9, 0),
                is_calendar_feed=0, classification_note='프로필 링크 경유 안내',
                product_name='라무르 수분크림', link_location='설명_프로필안내',
                url_type='링크모음', candidate_url='https://litt.ly/momma?x=1',
                link_status='unresolved', link_note='후보 전부 다른 상품', sort_order=0,
                sd=datetime.date(2026, 8, 7), ed=datetime.date(2026, 8, 13), stage='진행중',
                updated_at=datetime.datetime(2026, 8, 9, 3, 0), sib_n=3, sib_done_n=1)
    base.update(kw)
    return base


def _src(**kw):
    s = {'caption': {('ig', 'POST1'): '신청하면 오픈 후 링크 DM으로 보내드려요'},
         'bio': {'mom_market': ['https://litt.ly/momma']},
         'state': {'ig:POST1:0': {'attempts': 3, 'next_due': None, 'checked_at': '2026-08-09'}}}
    s.update(kw)
    return s


def test_shape가_한줄에_필요한_값을_모두_만든다():
    r = ub.shape(_db_row(), _src(), datetime.date(2026, 8, 10))
    assert r['dday'] == 3 and r['dday_label'] == 'D-3' and r['dday_bucket'] == 'D-7이내'
    assert r['cand_host'] == 'litt.ly' and r['has_cand'] == '후보있음'
    assert r['sib_label'] == '형제3(done 1)' and r['calendar'] == '일반'
    assert r['retry_state'] == '은퇴(소진)' and r['retry_n'] == 3   # rescan 이력 키가 맞물린다
    assert r['caption'].startswith('신청하면') and r['bio'] == ['https://litt.ly/momma']
    assert r['profile_url'] == 'https://www.instagram.com/mom_market/'
    assert r['publish'] == '2026-08-01' and r['status'] == 'unresolved'
    assert r['period'] == '08/07~08/13' and r['stage'] == '진행중'
    # 요약(summarize)이 세는 축은 모두 행에 실제로 존재해야 한다
    for key, _ in ub.SUMMARY_AXES:
        assert key in r, key


def test_shape_빈값들도_안전하다():
    r = ub.shape(_db_row(candidate_url=None, ed=None, link_status=None, url_type=None,
                         link_note=None, classification_note=None, sib_n=1, sib_done_n=0,
                         is_calendar_feed=1, native_id='OTHER'),
                 _src(), datetime.date(2026, 8, 10))
    assert r['status'] == ub.NULL_TOKEN and r['utype'] == '(없음)'
    assert r['cand_host'] == '(후보없음)' and r['has_cand'] == '후보없음'
    assert r['dday_label'] == '종료일미상' and r['sib_label'] == '단독'
    assert r['period'] == '08/07~?'          # 시작일만 있는 경우
    assert r['calendar'] == '달력피드' and r['caption'] == ''      # 캡션 없는 native_id
    assert r['retry_state'] == '미시도'                            # 이력 없는 키


def test_yt행은_영상링크와_채널링크를_쓴다():
    r = ub.shape(_db_row(platform='yt', native_id='VID1', owner_id='UCabc',
                         source_url='https://www.youtube.com/watch?v=VID1',
                         parent_title='8월 공구 모음', external_url='https://inpock.co.kr/ch'),
                 _src(), datetime.date(2026, 8, 10))
    assert r['profile_url'] == 'https://www.youtube.com/channel/UCabc'
    assert r['title'] == '8월 공구 모음' and r['external_url'] == 'https://inpock.co.kr/ch'
    assert r['bio'] == []          # 인스타 바이오 링크는 유튜브 행에 붙이지 않는다


# ------------------------------------------------------------------ HTML
def _html(rows=None):
    rows = rows if rows is not None else [ub.shape(_db_row(), _src(), datetime.date(2026, 8, 10))]
    return ub.render_html(rows, generated_at='2026-08-10 12:00')


def test_HTML이_자기완결이다():
    h = _html()
    assert h.startswith('<!DOCTYPE html>') and h.rstrip().endswith('</html>')
    # 외부 리소스를 전혀 안 불러온다(오프라인에서 그냥 열려야 함)
    for tag in ('src="http', "src='http", 'href="http://', '<link ', 'cdn'):
        assert tag not in h, tag
    assert '진행중 미해결 상품 진단 보드' in h
    for _, label in ub.SORTS:
        assert label in h


def test_HTML에_데이터가_박혀있고_칩필터_UI는_없다():
    h = _html()
    assert 'const ROWS = [' in h
    assert '라무르 수분크림' in h            # 한글은 이스케이프 없이 그대로(사람이 소스도 읽음)
    # 칩 필터는 걷어냈다(2026-08-10) — 흔적이 남아 있으면 죽은 코드다
    for gone in ('FACETS', 'renderFacets', 'class="chip', 'id="facets"', 'state.sel'):
        assert gone not in h, gone
    # 검색은 유일한 좁히기 수단이라 반드시 있어야 한다
    assert 'id="q"' in h and 'const HAY' in h


def test_한줄_열_구성이_CSS_머리글_행에서_모두_일치한다():
    """열을 하나 추가하면서 셋 중 하나만 고치면 표가 어긋난다 — 개수를 못박아둔다."""
    grid = re.search(r'\.row\{display:grid;grid-template-columns:([^;]+);', ub._CSS).group(1)
    n_css = len(grid.split())

    def n_spans(block):
        return block.count('<span')

    head = ub._JS.split('class="row head">')[1].split('</div>')[0]
    row = ub._JS.split('function rowHTML')[1].split('function detHTML')[0]
    assert n_css == n_spans(head) == n_spans(row) == 11
    for label in ('공구기간', '공구상태', 'link_status', '판단 이유(link_note)'):
        assert label in head, label


def test_기간_라벨():
    import datetime as dt
    assert ub.period_label(dt.date(2026, 8, 7), dt.date(2026, 8, 13)) == '08/07~08/13'
    assert ub.period_label(dt.date(2026, 8, 7), None) == '08/07~?'
    assert ub.period_label(None, dt.date(2026, 8, 13)) == '?~08/13'
    assert ub.period_label(None, None) == '기간미상'
    # DATETIME이 와도 날짜만 쓴다
    assert ub.period_label(dt.datetime(2026, 8, 7, 5), dt.date(2026, 8, 9)) == '08/07~08/09'


def test_스크립트_주입_방어():
    """캡션·상품명에 </script>나 주석 시작이 들어와도 스크립트 블록이 깨지지 않는다."""
    row = ub.shape(_db_row(product_name='</script><script>alert(1)</script>',
                           link_note='<!-- 주석 -->'), _src(), datetime.date(2026, 8, 10))
    h = _html([row])
    body = h.split('const ROWS = ')[1]
    assert '</script><script>' not in body.split('</script>')[0]
    assert '\\u003c/script' in h and '\\u003c!--' in h
    assert h.count('</script>') == 1        # 우리가 닫는 그 하나뿐


def test_행이_없어도_HTML이_나온다():
    h = _html([])
    assert 'const ROWS = []' in h and '상품 0건' in h


def test_요약줄은_모든_축을_한줄씩_찍는다():
    rows = [ub.shape(_db_row(), _src(), datetime.date(2026, 8, 10))]
    lines = ub.summarize(rows)
    assert len(lines) == len(ub.SUMMARY_AXES)
    assert any('플랫폼' in ln and 'ig=1' in ln for ln in lines)
