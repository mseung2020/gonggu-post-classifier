"""쿠팡/알리익스프레스/테무 원천 제외(2026-08-11 정책) — resolve 후보 단계에서 이 링크가
done이 되지 못하게 막는다. purge는 이미 적재된 것 청소용 일회성."""
from gonggu.resolve_links.antibot import is_excluded_marketplace
from gonggu.resolve_links.links import _filter_link_pairs


def test_is_excluded_marketplace_hosts():
    assert is_excluded_marketplace('https://www.coupang.com/vp/products/123')
    assert is_excluded_marketplace('https://link.coupang.com/a/xyz')      # 서브도메인
    assert is_excluded_marketplace('https://coupa.ng/abcd')               # 단축 도메인
    assert is_excluded_marketplace('https://s.click.aliexpress.com/e/_x') # 알리 서브도메인
    assert is_excluded_marketplace('https://www.temu.com/kr/g-1.html')


def test_not_excluded_normal_malls():
    assert not is_excluded_marketplace('https://smartstore.naver.com/x/products/1')
    assert not is_excluded_marketplace('https://item.gmarket.co.kr/Item?goodscode=1')
    assert not is_excluded_marketplace('https://shop.cafe24.com/p/1')
    assert not is_excluded_marketplace('')            # 빈 URL 안전


def test_filter_link_pairs_drops_marketplace():
    pairs = [
        ('https://smartstore.naver.com/x/products/1', '진짜 상품', 'link'),
        ('https://www.coupang.com/vp/products/9', '쿠팡 상품', 'link'),
        ('https://s.click.aliexpress.com/e/_z', '알리 상품', 'product'),
    ]
    out = _filter_link_pairs(pairs)
    hrefs = [c['href'] for c in out]
    assert 'https://smartstore.naver.com/x/products/1' in hrefs
    assert all('coupang' not in h and 'aliexpress' not in h for h in hrefs)
    assert len(out) == 1
