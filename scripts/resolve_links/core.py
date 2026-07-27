"""링크 해석의 핵심 상태 기계 — 후보 URL 하나하나를 시도하며 done/hold/unresolved/error를
가른다(post -> 프로필/링크모음 -> 상품 흐름의 오케스트레이션 본체)."""
from .antibot import is_non_mall
from .browser import fetch
from .config import BLOCKED_STATUS_CODES, BLOCKED_TEXT_MARKERS
from .links import extract_collection_links, linkbio_candidates, normalize_url, ordered_candidates
from .llm import judge_page
from .matching import hint_is_vague, post_context_text
from .picker import finalize_pick
from .urlutil import host_of
from .youtube import recover_truncated_url, youtube_channel_link

# _resolve_one_candidate()의 결과 status를 "이 정도면 최종으로 쓸 만한가" 순으로 매긴 순위.
# 후보를 여러 개 시도했는데 전부 done이 아니면, 그중 가장 나은 상태를 대표 결과로 남긴다
# (hold: 사람이 볼 근거는 있음 > unresolved: 못 찾음 > error: 크롤링/LLM 호출 자체가 실패).
_STATUS_RANK = {'error': 0, 'unresolved': 1, 'hold': 2}


def resolve_product(page, platform, parent, product):
    """candidate_url의 후보들을 순서대로 하나씩 시도하다가 처음 done이 나오면 즉시 반환한다.
    전부 실패하면 그중 가장 나은 상태를 반환. 반환: {status, final_url, note, tried_urls}
    (tried_urls는 실제로 시도한 URL 목록 — 나중에 "어떤 링크를 열어봤는지" 진단용)."""
    raw_urls = [u for u in (product.get('candidate_url') or '').split(';') if u]
    candidates = ordered_candidates(raw_urls, product.get('url_type'))

    if not candidates and platform == 'yt' and parent.get('video_id'):
        # candidate_url이 있었는데 전부 '...'로 잘려서 못 쓰게 된 경우, 유튜브 원문 설명에서
        # 안 잘린 전체 URL을 찾아 복구를 시도한다(채널링크 폴백보다 먼저 — 캡션에 있던 그
        # 링크 자체를 살리는 게 채널 전용 링크로 대체하는 것보다 더 정확한 신호이므로).
        for u in (u for u in raw_urls if '...' in u):
            recovered = recover_truncated_url(parent['video_id'], u)
            if recovered:
                candidates = ordered_candidates([recovered], product.get('url_type'))
                break

    if not candidates and platform == 'yt' and parent.get('channel_id'):
        # candidate_url이 원래 없었든, 있었는데 전부 잘려서(...) 못 쓰게 됐든 — 어차피 지금
        # 시도할 후보가 0개인 상황은 똑같으니 유튜브 채널 정보란의 링크를 마지막 수단으로
        # 시도한다(인스타의 프로필 external_url과 같은 역할). 성공/실패 여부와 무관하게
        # parent에 남겨서 gonggu_video.external_url로도 저장되게 한다.
        parent['external_url'] = youtube_channel_link(parent['channel_id'])
        if parent['external_url']:
            candidates = ordered_candidates([parent['external_url']], product.get('url_type'))

    if not raw_urls and not candidates:
        return {'status': 'unresolved', 'final_url': None, 'note': '크롤링할 후보 링크 없음', 'tried_urls': []}
    if not candidates:
        return {'status': 'unresolved', 'final_url': None,
                'note': f"실제 구매 링크(url_type={product.get('url_type')})가 원본부터 잘려서 확인 불가",
                'tried_urls': []}

    ctx = post_context_text(product, parent)
    tried_urls, best = [], None
    for url in candidates:
        norm_url = normalize_url(url)
        tried_urls.append(norm_url)
        res = _resolve_one_candidate(page, norm_url, product, ctx)
        if res['status'] == 'done':
            res['tried_urls'] = tried_urls
            return res
        if best is None or _STATUS_RANK.get(res['status'], -1) > _STATUS_RANK.get(best['status'], -1):
            best = res
    best['tried_urls'] = tried_urls
    return best


def _resolve_one_candidate(page, current_url, product, ctx):
    """후보 URL 하나에 대한 해석 시도. 반환: {status: done|unresolved|hold|error, final_url, note}"""
    # 인포크/litt.ly 등 알려진 링크인바이오 플랫폼이면 Playwright 없이 구조화 데이터로 먼저
    # 시도한다 — 실패/미지원이면 None이라 아래 기존 Playwright 경로로 그대로 넘어간다.
    fast_links = linkbio_candidates(current_url)
    if fast_links:
        return finalize_pick(page, fast_links, product, ctx, current_url, '링크인바이오(구조화)',
                              prefetched_final=True)

    r = fetch(page, current_url)
    if r['error']:
        return {'status': 'error', 'final_url': None, 'note': r['error']}

    if r['status'] in BLOCKED_STATUS_CODES:
        return {'status': 'unresolved', 'final_url': None,
                'note': f"로그인월_차단 — HTTP {r['status']} (안티봇/보안확인 페이지로 확인됨)"}
    if any(m.lower() in (r.get('body_text') or '').lower() for m in BLOCKED_TEXT_MARKERS):
        return {'status': 'unresolved', 'final_url': None,
                'note': f"로그인월_차단 — HTTP {r['status']}이지만 본문이 보안확인/캡차 문구로 확인됨"}

    page_info = {
        'url': r['final_url'],
        'host': host_of(r['final_url'] or current_url),
        'title': r['title'],
        'jsonld_name': r['jsonld'].get('name'),
        'jsonld_price': r['jsonld'].get('price'),
        'has_og_image': bool(r['jsonld'].get('image') or r['og_image']),
        'body_text_snippet': r.get('body_text', ''),
    }
    try:
        verdict = judge_page(ctx, page_info)
    except Exception as e:
        return {'status': 'error', 'final_url': None, 'note': f'LLM#3 호출 실패: {str(e)[:120]}'}

    if verdict.get('page_type') == '상품페이지' and verdict.get('is_final_product_page'):
        if is_non_mall(r['final_url']):
            return {'status': 'hold', 'final_url': r['final_url'],
                    'note': f"네이버 블로그({r['final_url']})는 몰이 아니라 상품/가격이 보여도 자동 확정하지"
                            f" 않음 — 사람 검토 필요"}
        if hint_is_vague(product.get('product_name')):
            return {'status': 'hold', 'final_url': r['final_url'],
                    'note': f"상품명(\"{product.get('product_name')}\")이 너무 일반적이라 이 상품페이지"
                            f"({r['title']})와의 일치를 자동으로 확정할 수 없음 — 사람 검토 필요"}
        return {'status': 'done', 'final_url': r['final_url'], 'note': (verdict.get('reason') or '')[:200]}

    page_type = verdict.get('page_type')
    if page_type in ('링크모음', '스토어메인'):
        links = extract_collection_links(page)
        if not links:
            return {'status': 'unresolved', 'final_url': None, 'note': f'{page_type}인데 후보 링크 추출 실패'}
        return finalize_pick(page, links, product, ctx, r['final_url'] or current_url, page_type,
                              prefetched_final=False)

    if page_type == '무관':
        # "무관"으로 판정된 것 중 일부는 명칭이 달라서 못 알아본 케이스일 수 있어 자동 실패
        # 종료 대신 사람이 검토할 "보류"로 뺀다.
        return {'status': 'hold', 'final_url': None, 'note': f"무관 — {(verdict.get('reason') or '')[:150]}"}

    # 로그인월_차단 / (상품페이지인데 원본과 불일치)
    return {'status': 'unresolved', 'final_url': None,
            'note': f"{page_type} — {(verdict.get('reason') or '')[:150]}"}
