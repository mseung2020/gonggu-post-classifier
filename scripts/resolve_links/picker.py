"""링크 후보 목록에서 LLM#2로 하나를 고르고, 확신도에 따라 확정/재검증/반려를 결정한다."""
from urllib.parse import urlparse

from .antibot import is_linkbio_hub, is_non_mall, looks_discontinued
from .browser import fetch
from .config import BLOCKED_STATUS_CODES, BLOCKED_TEXT_MARKERS, LINK_PICK_OK_CONF
from .links import normalize_url
from .llm import judge_page, pick_link
from .matching import hint_is_vague
from .redirect import follow_redirect
from .urlutil import host_of


def finalize_pick(page, links, product, ctx, referer, page_type_label, prefetched_final):
    """링크 후보 목록에서 LLM#2로 하나를 고르고, 확신도에 따라 확정한다.
    prefetched_final=True면 href가 이미 최종 목적지로 해석된 상태라서(예: linkbio_parser의
    구조화 데이터) follow_redirect로 다시 열어보지 않고 문자열 검증(판매종료/블로그)만 하고
    끝낸다 — False면(Playwright DOM에서 뽑은 raw href) 실제로 열어서 리다이렉트를 따라간다."""
    try:
        pick = pick_link(ctx, links)
    except Exception as e:
        return {'status': 'error', 'final_url': None, 'note': f'LLM#2 호출 실패: {str(e)[:120]}'}
    idx, confidence = pick.get('chosen_index', -1), pick.get('confidence')
    if idx is None or idx < 0 or idx >= len(links):
        # pick.get('reason')에 LLM#2가 왜 못 골랐는지(예: "아직 오픈 전이라 후보 링크 자체가
        # 없음")가 있는데 이걸 버리고 뭉뚱그려 쓰고 있었음 — 그대로 살려서 진단에 쓴다.
        reason = (pick.get('reason') or '').strip()
        note = f'LLM#2가 적합한 링크를 못 찾음: {reason[:150]}' if reason else 'LLM#2가 적합한 링크를 못 찾음'
        return {'status': 'unresolved', 'final_url': None, 'note': note}
    # 검증 홉이 없어진 뒤로는 여기서 확정하면 그대로 DB에 들어간다 — 예전엔 링크모음은
    # 확신도 무관하게 최선의 후보를 채택해도 LLM#3 재검증이 저확신 오판을 걸러줬지만, 이제는
    # 그 안전망이 없으므로 링크모음/스토어메인 둘 다 확신도가 낮으면(low) 자동 확정하지 않는다.
    if confidence not in LINK_PICK_OK_CONF:
        return {'status': 'unresolved', 'final_url': None,
                'note': f'{page_type_label} 후보 중 확신도 낮음(conf={confidence}) — 검증 홉이 없어서 오탐 방지로 채택 안 함'}
    chosen_href = normalize_url(links[idx]['href'])
    # linkbio_parser의 'links'(smart_store/collection처럼 상품명·가격이 구조화된 게 아니라
    # 그냥 버튼 하나)는 LLM#2가 "다른 후보가 없어서" 같은 이유로 conf=high를 줘도 실제로는
    # 스토어 메인이거나 또 다른 링크모음일 위험이 있다(실측 확인, 2026-07-21 — "윤남매맘
    # 공구쇼핑몰" 스토어 메인이 완전히 다른 상품 4개의 최종 링크로 확정됨). source='product'
    # (smart_store/collection, 실제 상품명·가격 있는 구조화 데이터)만 확신도 그대로 믿고,
    # 'link'는 확신도와 무관하게 아래 재검증 홉을 강제로 거친다.
    force_verify = prefetched_final and links[idx].get('source') == 'link'
    # conf=medium은 LLM#2 혼자 확정하기엔 애매해서(카테고리/매장 단위로 느슨하게 매칭했을
    # 위험) 실제 목적지 페이지까지 들어가 LLM#3로 한 번 더 판별한다 — 상품페이지+일치
    # 확인되면 확정, 아니면 버림. 이때 차단(로그인월/캡차)되면 URL 복구 시도 없이 그냥
    # 이 후보를 포기한다(내용을 못 본 채로 확정하지 않기 위함, 2026-07-20 결정).
    if confidence == 'medium' or force_verify:
        r2 = fetch(page, chosen_href, referer=referer)
        if r2['error']:
            return {'status': 'unresolved', 'final_url': None,
                    'note': f'{page_type_label} 후보(conf={confidence}) 재검증 중 접속 실패: {r2["error"]}'}
        if r2['status'] in BLOCKED_STATUS_CODES or any(
                m.lower() in (r2.get('body_text') or '').lower() for m in BLOCKED_TEXT_MARKERS):
            return {'status': 'unresolved', 'final_url': None,
                    'note': f'{page_type_label} 후보(conf={confidence}) 재검증 중 차단(로그인월/캡차) — 확인 불가로 포기'}
        page_info2 = {
            'url': r2['final_url'],
            'host': host_of(r2['final_url'] or chosen_href),
            'title': r2['title'],
            'jsonld_name': r2['jsonld'].get('name'),
            'jsonld_price': r2['jsonld'].get('price'),
            'has_og_image': bool(r2['jsonld'].get('image') or r2['og_image']),
            'body_text_snippet': r2.get('body_text', ''),
        }
        try:
            verdict2 = judge_page(ctx, page_info2)
        except Exception as e:
            return {'status': 'error', 'final_url': None, 'note': f'LLM#3 재검증 호출 실패: {str(e)[:120]}'}
        if not (verdict2.get('page_type') == '상품페이지' and verdict2.get('is_final_product_page')):
            return {'status': 'unresolved', 'final_url': None,
                    'note': f'{page_type_label} 후보(conf={confidence})를 LLM#3 재검증에서 반려 — '
                            f'{(verdict2.get("reason") or "")[:150]}'}
        if looks_discontinued(r2['final_url'] or chosen_href):
            return {'status': 'unresolved', 'final_url': None,
                    'note': f'{page_type_label} 후보(conf={confidence}) — 재검증한 페이지가 판매종료로 보임'}
        if is_non_mall(r2['final_url'] or chosen_href):
            return {'status': 'unresolved', 'final_url': None,
                    'note': f'{page_type_label} 후보(conf={confidence}) — 재검증한 페이지가 네이버 블로그(몰 아님)라 채택 안 함'}
        chosen_url, verify_note = r2['final_url'], (
            f"LLM#2 선택(conf={confidence}) + LLM#3 재검증 통과: {(verdict2.get('reason') or '')[:150]}")
    elif prefetched_final:
        # linkbio_parser가 이미 최종 목적지까지 리다이렉트를 추적해줬으니(예: inpock
        # /api/r/<토큰> -> 실제 스마트스토어 상품 URL) 다시 열어볼 필요 없다 — URL 문자열
        # 기반 검증(판매종료/블로그)만 하고 끝낸다. 여기 도달하는 건 이미 source='product'
        # (실제 상품명·가격 구조화 데이터)뿐이다 — 'link'는 위 force_verify로 다 걸러짐.
        # ⚠ 방어선: 도메인이 없는 깨진 URL(예: 리다이렉트 추적 실패로 상대경로가 그대로
        # 넘어온 경우, 실측 확인 2026-07-20 — "https:///api/r/..."가 그대로 done 확정됨)은
        # 여기서 한 번 더 걸러낸다. linkbio_candidates에서 이미 막았지만 이중 안전장치.
        if not urlparse(chosen_href).netloc:
            return {'status': 'unresolved', 'final_url': None,
                    'note': f'{page_type_label} 후보(conf={confidence})가 도메인 없는 깨진 URL이라 채택 안 함'
                            f' — {chosen_href[:150]}'}
        if looks_discontinued(chosen_href) or is_non_mall(chosen_href):
            return {'status': 'unresolved', 'final_url': None,
                    'note': f'{page_type_label} 후보(conf={confidence})가 판매종료/블로그 URL로 보여 채택 안 함'}
        # ⚠ 방어선 2: 고른 링크가 또 다른 링크인바이오 허브(인포크 등)면 "이미 최종
        # 목적지"라는 전제가 깨진다 — 중첩 구조라 검증 없이 확정하면 안 됨(실측 확인,
        # 2026-07-21 — 인포크 A의 버튼이 인포크 B를 가리켰는데 그대로 done 확정됨).
        if is_linkbio_hub(chosen_href):
            return {'status': 'unresolved', 'final_url': None,
                    'note': f'{page_type_label} 후보(conf={confidence})가 또 다른 링크모음 허브를 가리켜 채택 안 함'
                            f' — {chosen_href[:150]}'}
        chosen_url = chosen_href
        verify_note = (f"LLM#2 선택 채택(conf={confidence}, 링크인바이오 구조화 데이터): "
                        f"{(pick.get('reason') or '')[:150]}")
    else:
        # ⚠ "이 링크가 맞는 상품인지" 재검증(LLM#3)은 안 하지만, "이 링크가 실제로 열리는지"는
        # 확인해야 한다 — inpock 등 링크모음 서비스의 버튼 href가 자기네 내부 리다이렉트 API
        # (예: link.inpock.co.kr/api/r/<토큰>)를 가리키는 경우가 많은데, 이 URL을 referer 없이
        # 단독으로 열면 400이 나서 아예 안 열리는 죽은 링크가 된다(실측 확인, 2026-07-16) — 지금
        # 있던 페이지에서 온 것처럼 referer를 붙여서 한 번 더 열면(판단 없는 단순 리다이렉트
        # 추적) 정상적으로 진짜 목적지로 넘어간다.
        chosen_url, verified = follow_redirect(page, chosen_href, referer=referer)
        if not chosen_url:
            return {'status': 'unresolved', 'final_url': None,
                    'note': f'{page_type_label} 후보(conf={confidence})를 선택했지만 실제 목적지로 리다이렉트되지 않음'
                            f' — {chosen_href[:150]}'}
        verify_note = f"LLM#2 선택 채택(conf={confidence}): {(pick.get('reason') or '')[:150]}"
        if not verified:
            verify_note += ' (⚠ 로그인월/캡차라 URL만 복구했고 내용은 직접 확인 못함)'
    # hint_is_vague는 그대로 적용해서, 상품명이 너무 일반적인 경우(스토어메인 카탈로그에서
    # 뽑은 임의의 상품일 위험)는 자동 확정하지 않고 사람 검토로 돌린다.
    if hint_is_vague(product.get('product_name')):
        return {'status': 'hold', 'final_url': chosen_url,
                'note': f"상품명(\"{product.get('product_name')}\")이 너무 일반적이라 LLM#2 선택을"
                        f" 자동으로 확정할 수 없음 — 사람 검토 필요"}
    return {'status': 'done', 'final_url': chosen_url, 'note': verify_note}
