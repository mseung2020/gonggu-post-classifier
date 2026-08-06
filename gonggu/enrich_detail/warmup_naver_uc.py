#!/usr/bin/env python3
"""uc 전용 프로필에 네이버 신뢰를 한 번 쌓아두는 워밍업(2026-08-06).

배경: 스마트스토어가 이 IP/계정을 플래그해 자동화 접근마다 보안확인(영수증 캡차)/로그인월을
띄우는 상태. gonggu_scraper 개발자 방식대로 "전용 프로필 창에서 사람이 한 번 정상 접속(로그인/
캡차 통과)해 두면 이후 안정적"이다 — 그 1회를 이 스크립트로 한다. enrich_detail의 uc 엔진과
같은 프로필(data/auth/uc_profile)·같은 build_driver를 쓰므로, 여기서 통과해두면 이후
DETAIL_NAVER_ENGINE=uc 실행이 그 신뢰 쿠키를 그대로 물려받는다.

절차:
  1) 실제 크롬 창이 뜨고 스마트스토어 상품 페이지로 이동한다.
  2) 화면에 보안확인(영수증 문제)/로그인월이 뜨면 창에서 직접 통과한다(로그인까지 하면 더 좋다 —
     로그인 상태 유지 ON, IP 보안 OFF 권장).
  3) 상품 정보가 정상으로 보이면 이 터미널로 돌아와 Enter — 프로필에 쿠키가 저장된 채 창을 닫는다.

사용법(저장소 루트에서):
    python3 -m gonggu.enrich_detail.warmup_naver_uc
    python3 -m gonggu.enrich_detail.warmup_naver_uc "<확인할 스마트스토어 URL>"
"""
import sys

from . import naver_uc

DEFAULT_URL = 'https://smartstore.naver.com/main/products/'


def main():
    url = sys.argv[1] if len(sys.argv) > 1 else 'https://nid.naver.com/nidlogin.login'
    driver = naver_uc.build_driver()
    try:
        # 홈 → 쇼핑 워밍업 후 대상 페이지로(사람처럼 진입)
        for u in ('https://www.naver.com', 'https://search.shopping.naver.com/'):
            try:
                driver.get(u)
            except Exception:
                pass
        driver.get(url)
        print('\n[안내] 뜬 크롬 창에서 다음을 해주세요:')
        print('  1) 네이버 로그인(로그인 상태 유지 ON, IP 보안 OFF 권장)')
        print('  2) 보안확인(영수증 문제)이 뜨면 통과')
        print('  3) 스마트스토어 상품이 정상으로 보이면 아래 Enter')
        input('\n다 됐으면 Enter를 누르세요(프로필에 저장하고 창을 닫습니다)... ')
        print('프로필에 세션이 저장되었습니다. 이제 DETAIL_NAVER_ENGINE=uc 실행이 이 신뢰를 씁니다.')
    finally:
        try:
            driver.quit()
        except Exception:
            pass


if __name__ == '__main__':
    main()
