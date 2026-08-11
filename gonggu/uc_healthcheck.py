#!/usr/bin/env python3
"""데일리 시작 시 네이버 uc 신뢰(로그인 쿠키) 상태를 **비대화형**으로 점검한다(2026-08-11).

uc를 resolve/rescan의 기본 tier로 상시화하면서, 매 데일리 앞에 "지금 uc로 네이버가 뚫리는가"를
한 번 확인해 둔다. 로그인월/캡차가 뜨면 경고만 남기고(데일리는 계속 진행) — 그날 네이버 건은 uc가
통과 못 해 unresolved로 남고 다음 실행에 재시도된다. 쿠키를 갱신하려면 사람이 한 번:
    python3 -m gonggu.enrich_detail.warmup_naver_uc

⚠ 항상 exit 0 — 이 점검이 데일리를 멈추면 안 된다(무인 안전). UC_LOGIN_WAIT=0을 강제해 사람을
기다리지 않는다. 실제 크롬 창이 잠깐 떴다 닫힌다(화면 세션이 있는 맥에서 데일리를 돌린다는 전제).

사용법:
    python3 -m gonggu.uc_healthcheck
    UC_HEALTHCHECK_URL="<확인할 스마트스토어 URL>" python3 -m gonggu.uc_healthcheck
"""
import os

DEFAULT_URL = 'https://smartstore.naver.com/main'


def main():
    os.environ.setdefault('UC_LOGIN_WAIT', '0')  # 사람 안 기다림(비대화형)
    url = os.environ.get('UC_HEALTHCHECK_URL', DEFAULT_URL)
    try:
        from gonggu.uc_engine import close_sync, fetch_sync, looks_challenged
        final_url, html = fetch_sync(url)
    except Exception as e:
        print(f'  ⚠ uc 엔진 실행 실패 — 이번 실행에서 네이버 건을 못 뚫을 수 있습니다: {str(e)[:140]}')
        print('     크롬/드라이버 상태 점검 또는 워밍업 재실행: python3 -m gonggu.enrich_detail.warmup_naver_uc')
        return
    try:
        blocked = (not html) or 'nid.naver.com' in (final_url or '') or looks_challenged(html[:8000])
        if blocked:
            print('  ⚠ 네이버 uc 신뢰 만료(로그인월/캡차 감지) — 이번 실행의 네이버 건은 unresolved로')
            print('     남을 수 있습니다. 쿠키 갱신(사람 1회): python3 -m gonggu.enrich_detail.warmup_naver_uc')
        else:
            print('  ✓ 네이버 uc 신뢰 정상 — resolve/rescan에서 네이버·오픈마켓을 uc로 통과합니다.')
    finally:
        close_sync()  # 다음 단계(resolve)가 자기 프로세스에서 새 드라이버를 깨끗이 띄우게 이 창은 닫는다


if __name__ == '__main__':
    main()
