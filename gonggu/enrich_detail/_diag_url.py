#!/usr/bin/env python3
"""단건 URL 진단 — 특정 상품페이지에서 크롤링/추출이 왜 그렇게 나왔는지 눈으로 확인한다.

파이프라인 체크포인트(DB detail_status)는 일절 건드리지 않는다. 각 URL에 대해:
  1) fetch_detail_page 실제 경로(http/browser)로 크롤링
  2) HTML 스냅샷을 data/output/_detail_diag/에 저장 — 새 쇼핑몰 유형의 추출기를 보강할 때
     이 스냅샷을 그대로 분석 재료로 쓴다(같은 페이지를 다시 크롤링할 필요 없음)
  3) extract_facts 결과(소스/가격/이미지 수/본문 앞부분)를 콘솔에 요약

사용(저장소 루트에서):
    python3 -m gonggu.enrich_detail._diag_url "<url1>" "<url2>" ...
"""
import re
import sys

from playwright.sync_api import sync_playwright

from gonggu.common import ROOT
from gonggu.resolve_links.browser import new_context_page
from gonggu.resolve_links.config import AUTH_STATE_FILE

from .config import PAGE_TEXT_LIMIT
from .extract import extract_facts, is_list_url
from .fetchpage import fetch_detail_page

OUT_DIR = ROOT / 'data/output/_detail_diag'


def main():
    urls = sys.argv[1:]
    if not urls:
        sys.exit('사용법: python3 -m gonggu.enrich_detail._diag_url "<url>" ...')
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f'네이버 세션 파일: {"있음" if AUTH_STATE_FILE.exists() else "없음 — 네이버 계열이 429면 python3 -m gonggu.login_naver 먼저"}')

    with sync_playwright() as pw:
        browser, ctx, page = new_context_page(pw)
        try:
            for i, url in enumerate(urls, 1):
                print(f'\n=== [{i}/{len(urls)}] {url[:100]}')
                rec = fetch_detail_page(page, url)
                print(f'  경로={rec.get("via")} status={rec.get("status")} gone={rec.get("gone")} '
                      f'error={rec.get("error")} 목록URL={is_list_url(url)}')
                if not rec.get('html'):
                    continue
                name = re.sub(r'[^0-9A-Za-z.\-]+', '_', (rec.get('final_url') or url))[:80]
                snap = OUT_DIR / f'{i:02d}_{name}.html'
                snap.write_text(rec['html'], encoding='utf-8')
                print(f'  스냅샷: {snap.relative_to(ROOT)} ({len(rec["html"]):,}자)')
                f = extract_facts(rec['html'], rec.get('final_url') or url, PAGE_TEXT_LIMIT)
                print(f'  추출소스={f["source"]} 솔루션={f["solution"]} 상품명={str(f["product_name"])[:50]}')
                print(f'  가격: sale={f["sale_price"]} original={f["original_price"]} '
                      f'rate={f["discount_rate"]}({f["discount_source"]})')
                print(f'  배송: {f["shipping_note"]} fee={f["shipping_fee"]} free={f["free_shipping"]}')
                print(f'  이미지: 썸네일 {len(f["thumbnail_urls"])}장 / 상세 {len(f["detail_image_urls"])}장')
                print(f'  본문[:200]: {f["body_text"][:200]!r}')
        finally:
            browser.close()


if __name__ == '__main__':
    main()
