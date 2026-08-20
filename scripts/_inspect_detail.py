#!/usr/bin/env python3
"""일회성 점검 — 최근 채워진 상세(detail) 행을 사람이 읽기 좋게 덤프한다(읽기 전용, DB 안 건드림).
플래시 vs 프로 품질을 눈으로 비교/판단할 때 쓴다.

사용법(저장소 루트에서):
    python3 -m gonggu._inspect_detail            # 최근 20건(플랫폼 합쳐)
    N=40 python3 -m gonggu._inspect_detail        # 최근 40건
    STATUS=done python3 -m gonggu._inspect_detail # done만
"""
import os

from gonggu.common import connect_dst
from gonggu.platforms import PLATFORMS


def _detail_table(p):
    return f'{p.product_table}_detail'


def _fk(p):
    return p.product_table.replace('gonggu_', '') + '_id'


def _rows_sql(p, status_filter):
    where = "WHERE d.detail_status = %s" if status_filter else ""
    return f"""
SELECT pp.product_name, d.detail_status, d.original_price, d.sale_price, d.discount_rate,
       d.free_shipping, d.shipping_fee, d.composition_info, d.gift_info, d.coupon_info,
       d.category, d.subcategory, d.ai_summary, d.ai_summary_confidence, d.updated_at
FROM {_detail_table(p)} d
JOIN {p.product_table} pp ON pp.id = d.{_fk(p)}
{where}
ORDER BY d.updated_at DESC
LIMIT %s
""", where


def _fmt_price(o, s, r):
    if s is None and o is None:
        return '가격 없음'
    parts = []
    if o and s and o != s:
        parts.append(f'{o:,}→{s:,}원')
    elif s:
        parts.append(f'{s:,}원')
    elif o:
        parts.append(f'정가 {o:,}원')
    if r:
        parts.append(f'{r}%')
    return ' '.join(parts)


def main():
    n = int(os.environ.get('N', '20'))
    status = os.environ.get('STATUS') or None
    conn = connect_dst()
    rows = []
    try:
        with conn.cursor() as cur:
            for code, p in PLATFORMS.items():
                sql, where = _rows_sql(p, status)
                params = (status, n) if where else (n,)
                cur.execute(sql, params)
                for r in cur.fetchall():
                    r['_platform'] = code
                    rows.append(r)
            # 전체 상태 분포도 같이 (속도/건강 신호)
            dist = {}
            for code, p in PLATFORMS.items():
                cur.execute(f'SELECT detail_status, COUNT(*) c FROM {_detail_table(p)} GROUP BY detail_status')
                for r in cur.fetchall():
                    dist[r['detail_status']] = dist.get(r['detail_status'], 0) + r['c']
    finally:
        conn.close()

    rows.sort(key=lambda r: str(r['updated_at']), reverse=True)
    rows = rows[:n]

    print(f'=== 상세 테이블 상태 분포(전체): {dist} ===\n')
    print(f'=== 최근 {len(rows)}건 (updated_at 내림차순){f", status={status}" if status else ""} ===\n')
    for r in rows:
        price = _fmt_price(r['original_price'], r['sale_price'], r['discount_rate'])
        ship = ('무료배송' if r['free_shipping'] == 1
                else (f"배송 {r['shipping_fee']:,}원" if r['shipping_fee'] is not None else '배송 미상'))
        extras = ' | '.join(x for x in [
            f"구성:{r['composition_info']}" if r['composition_info'] else '',
            f"사은품:{r['gift_info']}" if r['gift_info'] else '',
            f"쿠폰:{r['coupon_info']}" if r['coupon_info'] else '',
            f"{r['category']}>{r['subcategory']}" if r['category'] else '',
        ] if x)
        print(f"[{r['detail_status']}] ({r['_platform']}) {(r['product_name'] or '')[:40]}")
        print(f"    {price}  |  {ship}" + (f"  |  {extras}" if extras else ''))
        if r['ai_summary']:
            print(f"    요약: {r['ai_summary'][:120]}")
        print()


if __name__ == '__main__':
    main()
