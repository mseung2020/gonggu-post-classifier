"""candidate_url에 후보가 여러 개일 때 "어느 순서로 열어볼지" + "다 실패해도 대표로 남길 후보
1개"를 크롤링 전에 순수 규칙으로 정한다(LLM 호출 없음, core.resolve_product 진입 전 단계).

실측 근거(2026-07-29, mimchoi.official 외 다중 후보 샘플 10건 수작업 검증):
  - 계정 핸들과 URL 슬러그가 정확히 같으면(예: 핸들 pullkim_ ↔ my.wiredy.io/pullkim_) 도메인이
    알려진 링크인바이오 허브가 아니어도 거의 항상 정답이었다 — 그래서 핸들 일치가 tier보다
    우선순위가 높다.
  - 반대로 핸들이 일치해도 계정이 최근에 링크인바이오 서비스를 바꿨거나 개명한 경우(예:
    link.inpock.co.kr/bbomm__02 대신 실제로는 litt.ly/susu0407이 정답) 핸들 일치가 틀리는
    경우도 있었다 — 이런 진짜 동률은 규칙으로 못 맞히므로 순서만 정하고, 실제 크롤링·검증은
    core.py가 후보 전부를 순서대로 계속 시도하게 그대로 둔다(여기서 하나로 강제 확정하지 않음).
  - forms.gle/카카오 인증 등 명백히 상품과 무관한 도메인(config.BAD_DOMAINS)은 아예 시도할
    가치가 없으므로 후보 목록에서 완전히 제거한다(저장도, 재시도도 안 함).
"""
from urllib.parse import urlparse

from .antibot import is_linkbio_hub, is_non_mall
from .config import BAD_DOMAINS, MALL_DOMAINS

_TIER_HUB, _TIER_MALL, _TIER_AMBIGUOUS, _TIER_NON_MALL = 0, 1, 2, 3


def _is_hard_excluded(url):
    return any(d in url for d in BAD_DOMAINS)


def _tier(url):
    if is_linkbio_hub(url):
        return _TIER_HUB
    if urlparse(url).hostname in MALL_DOMAINS:
        return _TIER_MALL
    if is_non_mall(url):
        # 네이버 블로그 등 — 몰은 아니지만 다른 후보가 전혀 없을 때의 최후 수단으로는 남겨둔다.
        return _TIER_NON_MALL
    return _TIER_AMBIGUOUS


def _dedup_key(url):
    """쿼리스트링/트레일링 슬래시만 다른 사실상 동일한 후보를 하나로 합친다(실측:
    ozip.me/ZrEcYOm vs ozip.me/ZrEcYOm?af). 경로 자체가 다르면(예: 끝의 언더바 유무로 계정
    슬러그 자체가 다른 kkang_twins vs kkang_twins_) 별개 후보로 취급해야 하므로 경로는
    건드리지 않는다."""
    p = urlparse(url)
    return f'{p.scheme}://{p.netloc.lower()}{p.path.rstrip("/")}'


def _handle_slug(url):
    parts = [p for p in urlparse(url).path.split('/') if p]
    return parts[0] if parts else ''


def _normalize_handle(s):
    # 언더바는 그대로 둔다 — kkang_twins(오답)와 kkang_twins_(정답)처럼 언더바 유무 자체가
    # 서로 다른 계정을 가리키는 실제 구분자였다(실측 확인, 2026-07-29). 앞의 '@'과 좌우
    # 공백만 정리한다.
    return (s or '').strip().lstrip('@').lower()


def handle_matches(url, handle):
    """URL의 첫 경로 조각(계정 슬러그로 보임)이 계정 핸들과 사실상 같은 문자열인지."""
    if not handle:
        return False
    slug = _normalize_handle(_handle_slug(url))
    nh = _normalize_handle(handle)
    return bool(slug) and bool(nh) and slug == nh


def rank_candidates(urls, handle=None):
    """원본 후보 URL 목록을 (핸들 일치 > 링크인바이오 허브 > 확정몰 > 그 외 > 비몰) 순으로
    정렬해 반환한다. 상품과 무관한 게 명백한 도메인은 아예 제거한다. 순서만 정할 뿐 최종
    확정은 하지 않는다 — core.py가 이 순서대로 실제로 열어보며 검증한다."""
    seen, deduped = set(), []
    for u in urls or []:
        # "..."로 잘린 링크(캡션 원본부터 잘려서 우리가 고칠 방법이 없는 것)는 애초에 열어볼
        # 수 없으니 제외한다 — core.resolve_product이 이 경우 유튜브 복구/채널링크 폴백을 시도.
        if not u or '...' in u or _is_hard_excluded(u):
            continue
        key = _dedup_key(u)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(u)

    def sort_key(item):
        idx, url = item
        base = 0 if handle_matches(url, handle) else 1
        return (base, _tier(url), idx)

    ranked = sorted(enumerate(deduped), key=sort_key)
    return [url for _, url in ranked]
