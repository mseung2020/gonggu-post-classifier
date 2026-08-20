"""case_matrix(케이스 세분화 리포트)의 DB 없이 검증 가능한 부분 — SQL 조립과 표 집계.

리포트 전용 모듈이라 파이프라인 판정에는 영향이 없지만, 축을 늘리다가 SQL이 깨지거나
축 목록과 SELECT의 별칭이 어긋나는 사고는 막아둔다.
"""
import collections

from gonggu import case_matrix as cm


def test_모든_축과_OX플래그가_SELECT에_별칭으로_존재한다():
    sql = cm._axis_sql()
    for col, _ in cm.AXES:
        if col == 'platform':      # platform은 flat SELECT에서 이미 나온다
            continue
        assert f'AS {col}' in sql, f'{col} 별칭이 SELECT에 없음'
    for col, _ in cm.OX_FLAGS:
        assert f'AS {col}' in sql, f'{col} 별칭이 SELECT에 없음'


def test_OX플래그마다_정의문이_있다():
    for col, _ in cm.OX_FLAGS:
        assert cm._OX_DEF.get(col), f'{col}의 O 정의(_OX_DEF)가 비어있음'


def test_조합축은_모두_AXES에_정의된_축이다():
    known = {c for c, _ in cm.AXES}
    assert set(cm.COMBO_CORE) <= known
    assert set(cm.COMBO_FULL) == known
    for entry in cm.CROSSTABS:
        rax, cax = entry[0], entry[1]
        assert rax in known and cax in known
        if len(entry) > 3:
            assert entry[3] in cm._CROSSTAB_FILTERS, f'{entry[3]} 필터 키가 정의 안 됨'


def test_LIKE절_조립():
    assert cm._any_like('u', ['a.b', 'c']) == "(u LIKE '%a.b%' OR u LIKE '%c%')"
    assert cm._no_url('x') == "(x IS NULL OR x = '')"


def test_두_플랫폼_SELECT의_컬럼수가_같다():
    """UNION ALL이 성립해야 하므로 두 SELECT의 컬럼 수가 같아야 한다."""
    flat = cm._flat_sql().strip()[1:-1]   # 바깥 괄호를 벗긴다
    ig, yt = flat.split('UNION ALL')

    def n_cols(part):
        # 괄호 안(스칼라 서브쿼리)을 먼저 지운 뒤 최상위 콤마만 센다.
        out, depth = [], 0
        for ch in part:
            if ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
            elif depth == 0:
                out.append(ch)
        head = ''.join(out).split('FROM gonggu_')[0]
        return head.count(',') + 1

    assert n_cols(ig) == n_cols(yt) == 39


def _fake_rows():
    base = {c: 'x' for c, _ in cm.AXES}
    base.update({c: 0 for c, _ in cm.OX_FLAGS})
    base.update({'platform': 'ig', 'parent_key': 'p1', 'link_status': 'unresolved'})
    r1 = dict(base)
    r2 = dict(base, parent_key='p2', c_period='A_시작O_종료O', ox_start=1)
    r3 = dict(base, parent_key='p2', c_period='A_시작O_종료O', ox_start=1)
    return [r1, r2, r3]


def test_분포와_조합_집계():
    rows = _fake_rows()
    assert dict(cm._dist(rows, 'c_period')) == {'A_시작O_종료O': 2, 'x': 1}
    combo = cm._combo(rows, ['c_period'])
    assert combo[0] == (('A_시작O_종료O',), 2)


def test_리포트_생성이_예외없이_끝나고_핵심문구를_포함한다():
    md = cm.build_report(_fake_rows())
    assert '케이스 전수 세분화 리포트' in md
    assert '## 0. 세분화 축 정의서' in md
    assert '## 3. O/X 조합 표' in md
    # O/X 표의 머리글이 OX_FLAGS 순서대로 들어있다
    for _, header in cm.OX_FLAGS:
        assert header in md


def test_url_type_교차표는_done과_미해결을_분리해서_집계한다():
    """url_type은 원본 후보 판정을 write-once로 유지하는데 candidate_url은 resolve_links가
    done으로 확정하는 순간 최종 도착 URL로 덮어쓴다(2026-08-18) — 그래서 done 행은 url_type과
    도메인이 달라도 정상이다. 두 표가 섞이지 않고 각자의 부분집합만 세는지 확인한다."""
    base = {c: 'x' for c, _ in cm.AXES}
    base.update({c: 0 for c, _ in cm.OX_FLAGS})
    base.update({'platform': 'ig', 'parent_key': 'p1'})
    # done 2건(허브→몰로 정상 귀결, url_type과 c_urlkind 불일치) + 미해결 1건(진짜 오분류 후보)
    r_done1 = dict(base, parent_key='p1', link_status='done',
                   c_urltype='링크모음', c_urlkind='05_네이버_스마트·브랜드스토어')
    r_done2 = dict(base, parent_key='p2', link_status='done',
                   c_urltype='링크모음', c_urlkind='12_자사몰(빌더도메인)')
    r_unresolved = dict(base, parent_key='p3', link_status='unresolved',
                         c_urltype='링크모음', c_urlkind='05_네이버_스마트·브랜드스토어')
    rows = [r_done1, r_done2, r_unresolved]

    md = cm.build_report(rows)
    assert '미해결만(done 제외' in md
    assert 'link_status=done만' in md
    assert '(해당 2건 대상)' in md   # done 표
    assert '(해당 1건 대상)' in md   # 미해결 표


def test_emit_sql이_뷰와_예제쿼리를_만든다(tmp_path):
    path = tmp_path / 'case_matrix.sql'
    cm.emit_sql(path)
    text = path.read_text(encoding='utf-8')
    assert 'CREATE OR REPLACE VIEW v_gonggu_case_axes AS' in text
    assert text.count('FROM v_gonggu_case_axes') >= len(cm.AXES) + len(cm.CROSSTABS)
    assert 'CREATE TABLE' not in text and 'DROP' not in text  # 읽기 전용 보장
    assert 'UPDATE ' not in text and 'DELETE ' not in text
