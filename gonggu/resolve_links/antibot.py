"""'이 URL을 최종 링크로 확정해도 되는가'를 판단하는 방어 규칙들 — 실제 라이브 실행 중
발견된 오탐 사례를 코드 레벨에서 막기 위한 것들이라, 함수 하나하나가 사고 하나씩과 대응된다."""
import re
from urllib.parse import parse_qs, urlparse

from gonggu import linkbio_parser

from .config import (BROKEN_PATH_SEGMENTS, DISCONTINUED_MARKERS, EXCLUDED_MARKETPLACE_DOMAINS,
                     NON_MALL_DOMAINS, UC_LOGINWALL_HOSTS)
from .urlutil import host_of


def is_non_mall(url):
    return host_of(url) in NON_MALL_DOMAINS


def is_uc_host(url):
    """네이버/오픈마켓처럼 Playwright엔 로그인월·403/429로 막혀 uc 패스가 처리해야 하는 호스트인지.
    부분 문자열 매칭(reverify_uc의 RESOLVE_UC_HOSTS와 동일 규칙: 'naver.'가 host에 들어가면 참)."""
    h = host_of(url or '')
    return any(k in h for k in UC_LOGINWALL_HOSTS)


def is_excluded_marketplace(url):
    """쿠팡/알리익스프레스/테무 — 제휴·오픈마켓이라 공구 대상에서 원천 제외(2026-08-11 정책).
    host가 도메인과 정확히 같거나 서브도메인(.으로 끝나는 매칭)일 때 True — 예: link.coupang.com,
    s.click.aliexpress.com. purge_marketplace_links와 같은 매칭 규칙."""
    h = host_of(url or '')
    return any(h == d or h.endswith('.' + d) for d in EXCLUDED_MARKETPLACE_DOMAINS)


def is_linkbio_hub(url):
    """이 URL이 인포크/litt.ly 등 또 다른 링크인바이오 허브를 가리키는지 — prefetched_final
    경로에서 "이미 최종 목적지"라는 전제가 깨지는 중첩 구조를 잡아내기 위함."""
    try:
        linkbio_parser.detect_platform(url)
        return True
    except ValueError:
        return False


def looks_discontinued(url):
    """URL 경로/쿼리 자체에 판매종료·에러 신호가 있으면 검증 없이도 걸러낸다. 이건 페이지
    내용을 다시 판단하는 게 아니라 URL 문자열 자체의 결정론적 신호라, "검증 홉 없이
    확정한다"는 정책과 충돌하지 않는다."""
    lower = url.lower()
    if any(m in lower for m in DISCONTINUED_MARKERS):
        return True
    return urlparse(lower).path.strip('/') in BROKEN_PATH_SEGMENTS


def recover_from_block(url):
    """차단/로그인월 리다이렉트 URL에서 원래 목적지를 복구할 수 있으면 복구한다. 실측으로
    확인된 두 패턴(2026-07-20):
    - 네이버 로그인월: nid.naver.com/nidlogin.login?url=<인코딩된 목적지> — 네이버가 로그인
      리다이렉트에 원래 목적지를 그대로 노출해줌.
    - Cloudflare 챌린지(예: item.gmarket.co.kr): 원래 요청 URL 뒤에 &__cf_chl_rt_tk=<토큰>만
      추가로 붙는 방식이라, 그 파라미터만 떼면 원래 요청한 URL 그대로임.
    둘 다 우리가 직접 그 페이지 내용을 본 건 아니라 신뢰도는 100%는 아니지만, 어차피 카카오
    오픈채팅 같은 진짜 복구 불가능한 경우와는 구분해서 살릴 가치가 있음. 복구 불가능하면 None."""
    parsed = urlparse(url)
    if 'nid.naver.com' in parsed.netloc:
        target = parse_qs(parsed.query).get('url', [None])[0]
        return target or None
    if '__cf_chl_rt_tk' in url:
        return re.sub(r'[?&]__cf_chl_rt_tk=[^&]*', '', url)
    return None
