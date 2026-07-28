"""판단(LLM) 없이 "이 링크가 실제로 어디로 이동하는지"만 확인하는 리다이렉트 추적."""
from .antibot import is_non_mall, looks_discontinued, recover_from_block
from .browser import domain_gate
from .config import BAD_DOMAINS


def follow_redirect(page, url, referer):
    """판단(LLM) 없이 그냥 한 번 더 열어서 진짜 목적지 URL만 얻는다 — "이 상품이 맞는지"는 안
    보고 "이 링크가 실제로 열리는지"만 확인. referer를 원래 있던 페이지로 지정해야 하는 이유는
    호출부 주석 참고. ⚠ "URL이 바뀌었는지"로 성공/실패를 판단하면 안 된다 — 애초에 리다이렉트가
    필요 없는(이미 최종 목적지인) 링크를 전부 실패로 오판하게 된다(실측으로 발견, 2026-07-16).
    반환: (최종 url 또는 None, verified) — verified=False면 우리가 직접 그 페이지 내용을
    확인하지는 못했지만(로그인월/캡차 등) URL 자체는 복구한 경우."""
    try:
        with domain_gate(url):
            resp = page.goto(url, referer=referer, wait_until='domcontentloaded', timeout=20000)
            try:
                page.wait_for_load_state('networkidle', timeout=6000)
            except Exception:
                pass
    except Exception:
        return None, False
    final_url = page.url
    status = resp.status if resp is not None else None
    is_bad_domain = any(d in final_url for d in BAD_DOMAINS)
    if status is not None and status < 400 and not is_bad_domain:
        if looks_discontinued(final_url) or is_non_mall(final_url):
            return None, False
        return final_url, True
    if is_bad_domain:
        # 로그인월/카카오 오픈채팅 등 그 자체는 못 쓰는 목적지 — URL에서 원래 목적지를 복구할
        # 수 있을 때만(예: nid.naver.com의 url= 파라미터) 살리고, 안 되면 완전히 실패.
        recovered = recover_from_block(final_url)
        if recovered and (looks_discontinued(recovered) or is_non_mall(recovered)):
            return None, False
        return recovered, False
    # BAD_DOMAINS는 아닌데 상태코드가 4xx/5xx인 경우(Cloudflare 등 안티봇). 원래 요청한 URL과
    # 아예 같으면(예: referer 없는 inpock api/r/ 400처럼 이동 자체가 안 된 경우) 진짜 실패.
    # 달라졌다면 어딘가로는 이동은 했다는 뜻이라 그 목적지 URL 자체를 신뢰한다 — Cloudflare
    # 챌린지는 URL에 흔적(__cf_chl_rt_tk)을 남기기도 하고 안 남기기도 해서(실측 확인,
    # 2026-07-20) 패턴 매칭만으로는 못 잡고, "이동했는지"가 더 안정적인 신호였음.
    if final_url.split('#')[0] == url.split('#')[0]:
        return None, False
    recovered = recover_from_block(final_url) or final_url
    if looks_discontinued(recovered) or is_non_mall(recovered):
        return None, False
    return recovered, False
