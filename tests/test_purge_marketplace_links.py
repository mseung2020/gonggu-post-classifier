"""purge_marketplace_links — 쿠팡/알리익스프레스/테무 최종 링크 상품 일괄 제거.

DB 없이 검증 가능한 세 가지를 못박는다.
  1) 마켓 매칭: 대표 도메인·서브도메인·단축링크·세미콜론 다중 후보 모두 잡고, 다른
     마켓(네이버/인포크 등)은 절대 안 걸린다.
  2) find_targets가 만드는 SELECT는 읽기 전용이고 link_status 필터가 옵션이다.
  3) delete_targets는 플랫폼별로 id를 모아 DELETE 한 번씩만 날린다(부모 테이블은 안 건드림).
"""
import re

import pytest

from gonggu import purge_marketplace_links as pm


# ------------------------------------------------------------------ 마켓 매칭
def test_대표_도메인_매칭():
    assert pm.marketplace_of('https://www.coupang.com/vp/products/123') == '쿠팡'
    assert pm.marketplace_of('https://ko.aliexpress.com/item/456.html') == '알리익스프레스'
    assert pm.marketplace_of('https://www.temu.com/kr/goods.html') == '테무'


def test_서브도메인과_단축링크도_잡는다():
    assert pm.marketplace_of('https://link.coupang.com/a/abcd') == '쿠팡'
    assert pm.marketplace_of('https://coupa.ng/abcd') == '쿠팡'
    assert pm.marketplace_of('https://s.click.aliexpress.com/e/xyz') == '알리익스프레스'
    assert pm.marketplace_of('https://m.temu.com/goods.html') == '테무'


def test_세미콜론_다중_후보_중_하나만_걸려도_매치():
    cand = 'https://smartstore.naver.com/a/products/1;https://www.coupang.com/vp/products/2'
    assert pm.marketplace_of(cand) == '쿠팡'


def test_다른_마켓은_매치_안됨():
    assert pm.marketplace_of('https://smartstore.naver.com/a/products/1') is None
    assert pm.marketplace_of('https://link.inpock.co.kr/momma') is None
    assert pm.marketplace_of('') is None
    assert pm.marketplace_of(None) is None


def test_유사_도메인은_오탐하지_않는다():
    """coupang.com.evil.example.com 같은 위장 호스트는 서브도메인이 아니라 상위 도메인이
    다르므로 매치되면 안 된다."""
    assert pm.marketplace_of('https://coupang.com.evil.example.com/x') is None
    assert pm.marketplace_of('https://notcoupang.com/x') is None


# ------------------------------------------------------------------ find_targets / delete_targets
class _FakeCursor:
    """execute()에서 SQL의 FROM 테이블명을 읽어 그 테이블에 대응하는 고정 행을 돌려준다.
    실제 DB 없이 find_targets/delete_targets의 조립 로직만 검증한다."""

    def __init__(self, rows_by_table, calls):
        self.rows_by_table = rows_by_table
        self.calls = calls
        self._last_rows = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if sql.strip().upper().startswith('DELETE'):
            table = re.search(r'DELETE FROM (\S+)', sql).group(1)
            ids = set(params or [])
            before = len(self.rows_by_table.get(table, []))
            self.rows_by_table[table] = [r for r in self.rows_by_table.get(table, [])
                                          if r['id'] not in ids]
            self.rowcount = before - len(self.rows_by_table[table])
            return
        table = re.search(r'FROM (\S+)', sql).group(1)
        self._last_rows = list(self.rows_by_table.get(table, []))
        if 'link_status IN' in sql:
            allowed = set(re.findall(r"'([^']*)'", sql.split('link_status IN')[1]))
            self._last_rows = [r for r in self._last_rows if r['link_status'] in allowed]

    def fetchall(self):
        return self._last_rows


class _FakeConn:
    def __init__(self, rows_by_table):
        self.rows_by_table = rows_by_table
        self.calls = []
        self.committed = False

    def cursor(self):
        return _FakeCursor(self.rows_by_table, self.calls)

    def commit(self):
        self.committed = True


def _row(id, native_id, candidate_url, link_status='done', name='상품'):
    return {'id': id, 'native_id': native_id, 'product_name': name,
            'candidate_url': candidate_url, 'link_status': link_status}


def test_find_targets는_마켓_매치된_행만_돌려준다():
    rows = {
        'gonggu_post_product': [
            _row(1, 'P1', 'https://www.coupang.com/vp/products/1'),
            _row(2, 'P2', 'https://smartstore.naver.com/a/products/2'),
        ],
        'gonggu_video_product': [
            _row(3, 'V1', 'https://www.temu.com/kr/goods.html', link_status='unresolved'),
        ],
    }
    conn = _FakeConn(rows)
    targets = pm.find_targets(conn)
    assert {(t['platform'], t['id'], t['market']) for t in targets} == {
        ('ig', 1, '쿠팡'), ('yt', 3, '테무'),
    }


def test_find_targets는_status_필터를_옵션으로_적용한다():
    rows = {
        'gonggu_post_product': [
            _row(1, 'P1', 'https://www.coupang.com/vp/products/1', link_status='done'),
            _row(2, 'P2', 'https://www.coupang.com/vp/products/2', link_status='unresolved'),
        ],
        'gonggu_video_product': [],
    }
    conn = _FakeConn(rows)
    targets = pm.find_targets(conn, statuses=('done',))
    assert [t['id'] for t in targets] == [1]
    # 필터를 안 주면 link_status 무관하게 전부 대상이다
    targets_all = pm.find_targets(_FakeConn(rows))
    assert {t['id'] for t in targets_all} == {1, 2}


def test_읽기_조회는_쓰기_구문이_없다():
    conn = _FakeConn({'gonggu_post_product': [], 'gonggu_video_product': []})
    pm.find_targets(conn)
    for sql, _ in conn.calls:
        upper = sql.upper()
        for bad in ('UPDATE ', 'INSERT ', 'DELETE ', 'DROP ', 'ALTER ', 'TRUNCATE'):
            assert bad not in upper


def test_delete_targets는_플랫폼별로_한번씩만_지운다():
    rows = {
        'gonggu_post_product': [_row(1, 'P1', 'x'), _row(2, 'P2', 'y'), _row(3, 'P3', 'z')],
        'gonggu_video_product': [_row(9, 'V1', 'w')],
    }
    conn = _FakeConn(rows)
    targets = [
        {'platform': 'ig', 'id': 1}, {'platform': 'ig', 'id': 2},
        {'platform': 'yt', 'id': 9},
    ]
    deleted = pm.delete_targets(conn, targets)
    assert deleted == {'ig': 2, 'yt': 1}
    assert conn.committed
    delete_calls = [c for c in conn.calls if c[0].strip().upper().startswith('DELETE')]
    assert len(delete_calls) == 2               # 플랫폼당 한 번(1건씩 여러 번이 아니라)
    # 지워지지 않은 행(id=3)은 그대로 남아 있어야 한다
    assert [r['id'] for r in rows['gonggu_post_product']] == [3]


def test_delete_targets는_대상이_없는_플랫폼은_건드리지_않는다():
    conn = _FakeConn({'gonggu_post_product': [_row(1, 'P1', 'x')], 'gonggu_video_product': []})
    deleted = pm.delete_targets(conn, [{'platform': 'ig', 'id': 1}])
    assert deleted == {'ig': 1}
    assert not any('gonggu_video_product' in c[0] for c in conn.calls)


def test_부모_테이블은_절대_건드리지_않는다():
    """gonggu_post/gonggu_video(부모)는 어떤 함수에서도 등장하면 안 된다."""
    conn = _FakeConn({'gonggu_post_product': [_row(1, 'P1', 'https://www.coupang.com/x')],
                      'gonggu_video_product': []})
    targets = pm.find_targets(conn)
    pm.delete_targets(conn, targets)
    for sql, _ in conn.calls:
        assert re.search(r'\bgonggu_post\b', sql) is None
        assert re.search(r'\bgonggu_video\b', sql) is None
