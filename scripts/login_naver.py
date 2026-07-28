#!/usr/bin/env python3
"""네이버에 직접 로그인한 뒤 그 세션(쿠키)을 저장해서, resolve_links의 모든 워커가 로그인된
상태로 스마트스토어/블로그 등에 접근하게 한다 — 익명 세션은 로그인월로 튕기는 페이지를
로그인 상태면 그대로 통과할 수 있어서, 안티봇 위험 없이(계정 자체가 실제로 로그인한 것이므로)
차단률을 줄이는 효과를 기대한다. 저장된 세션은 browser.py의 new_context_page가 자동으로
불러와 쓴다 — 이 스크립트를 다시 실행하지 않는 한 계속 재사용됨(만료되면 다시 실행).

브라우저 창이 뜨고 네이버 로그인 페이지로 이동한다. 직접 아이디/비번(+필요하면 2단계 인증)을
입력해서 로그인을 마친 뒤, 이 터미널로 돌아와 Enter를 누르면 그 시점의 쿠키를 저장한다.

⚠ data/auth/session_state.json에는 실제 로그인 쿠키가 그대로 들어있다 — .gitignore에서
data/auth/*를 이미 막아두었지만, 이 파일을 다른 곳에 복사/공유하지 말 것(계정 탈취 위험).

사용법:
    python3 scripts/login_naver.py
"""
from playwright.sync_api import sync_playwright

from resolve_links.config import AUTH_STATE_FILE


def main():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        ctx = browser.new_context(locale='ko-KR')
        page = ctx.new_page()
        page.goto('https://nid.naver.com/nidlogin.login')
        print('브라우저 창에서 네이버에 로그인하세요(2단계 인증 포함).')
        input('로그인을 마쳤으면 이 터미널로 돌아와 Enter를 누르세요...')

        AUTH_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        ctx.storage_state(path=str(AUTH_STATE_FILE))
        print(f'로그인 세션 저장됨 -> {AUTH_STATE_FILE}')
        print('다음 resolve_links 실행부터 이 세션을 자동으로 불러와 씁니다.')
        browser.close()


if __name__ == '__main__':
    main()
