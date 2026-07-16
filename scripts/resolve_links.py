#!/usr/bin/env python3
"""4.5단계(transform.py와 load.py 사이): load_ready.json의 각 상품이 가진 candidate_url
(LLM#1이 캡션/프로필에서 뽑은 원본 후보 링크들, 세미콜론으로 이어붙여진 상태)을 실제로
크롤링해서 "찐 최종 링크 하나"로 좁힌다.

흐름 (post -> 프로필/링크모음 -> 상품. 옛 gonggu-link-resolver/scripts/resolver.py의
크롤링 로직을 이 프로젝트의 상품 배열 스키마에 맞게 이식):
  candidate_url의 후보(세미콜론 구분) 중 url_type과 도메인이 맞는 것부터 순서대로 하나씩 시도
  (ordered_candidates) — 하나가 실패하면 다음 후보로 넘어가고, 하나라도 done이 나오면 즉시 확정.
  후보 하나에 대한 시도(_resolve_one_candidate):
    -> LLM#3(페이지판별): 도착한 페이지가 원본 포스트 상품의 "최종 상품페이지"인지 판별
       - 최종 상품페이지로 확정 -> candidate_url을 이 URL로 교체, done
       - 링크모음/스토어메인이면: 페이지의 링크 후보 추출(스크립트) -> LLM#2(링크선택)로 하나
         고름 -> 확신도(confidence)가 충분하면(LINK_PICK_OK_CONF) ⚠ 그 링크를 실제로 열어서
         재검증하지 않고 즉시 최종 후보로 확정, done (네이버 등 최종 목적지에서 자주 걸리는
         안티봇 차단을 원천적으로 피하기 위함 — 그 대신 판단의 무게중심을 LLM#2 쪽으로 옮겨서
         링크선택 프롬프트를 더 신중하게 다듬어둠)
       - 로그인월_차단/무관/확신도 낮은 링크선택 등 -> 이 후보는 실패, 다음 후보로
  모든 후보가 실패하면 그중 가장 나은 상태(hold > unresolved > error)를 반환하고, candidate_url은
  원본 후보 목록 그대로 유지. 실제로 시도한 URL들은 결과의 tried_urls에 남아서 나중에 진단 가능.

⚠ 마감/예정 등 진행 단계와 무관하게 항상 해석을 시도한다 — 공구가 끝났거나 아직 안 열렸어도
프로필의 링크모음(인포크 등)에 상품 링크가 걸려있을 수 있으므로 미리 걸러내지 않음.

⚠ 이 단계는 "링크를 하나로 확정"까지만 담당한다. 그 링크를 열어서 실제 가격/이미지/옵션 등
진짜 상품 데이터를 가져오는 것은 이 파이프라인 밖(다른 개발자 담당)이다 — 그래서 이 스크립트는
이미지 다운로드나 가격 저장을 하지 않는다(LLM#3 판별에 참고 신호로만 씀).

Dify API 키 2개 필요(.env):
  DIFY_KEY_PICK  — Dify 워크플로우 "공구왕 링크선택" (dify_workflows/02_link_selection.yml)
  DIFY_KEY_JUDGE — Dify 워크플로우 "공구왕 페이지판별" (dify_workflows/03_page_judge.yml)

사용법:
    python3 scripts/resolve_links.py            # load_ready.json 전체(아직 해석 안 된 상품만)
    python3 scripts/resolve_links.py 50         # 상품 단위로 50건만
체크포인트: data/output/link_resolution.json (10건마다 저장 — Ctrl+C로 중단해도 다시 실행하면
           이어서 진행됨)
결과: data/output/load_ready_resolved.json — load_ready.json과 같은 구조, candidate_url만
      해석 성공한 상품에 한해 최종 링크로 교체됨(실패/보류는 원본 후보 목록 그대로 유지)
"""
import json
import os
import re
import sys
import time
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

from common import LOAD_READY_FILE, RESOLVED_FILE, ROOT, call_dify, dump_json, load_json

RESOLUTION_FILE = ROOT / 'data/output/link_resolution.json'
AUTH_STATE_FILE = ROOT / 'data/auth/session_state.json'

DIFY_KEY_PICK = os.environ.get('DIFY_KEY_PICK', '')
DIFY_KEY_JUDGE = os.environ.get('DIFY_KEY_JUDGE', '')

UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36')

BAD_DOMAINS = ('nid.naver.com', 'accounts.kakao.com', 'account.kakao.com', 'mkt.shopping.naver',
               'pf.kakao.com', 'forms.gle', 'docs.google', 'canva.site', 'band.us',
               'instagram.com', 'youtube.com', 'youtu.be')
# 버튼 텍스트에 이런 말이 있으면 애초에 상품 구매 링크가 아니니 LLM#2한테 보여주지도 않고
# 후보에서 뺀다 — LLM#2 프롬프트에도 같은 취지의 지침이 있지만, 다른 후보가 다 별로면 그중
# "제일 나은" 걸로 고객센터/문의 링크를 골라버리는 경우가 실제로 있어서(확신도 낮게라도)
# 코드 레벨에서 원천적으로 제외한다.
NON_PRODUCT_TEXT = ('고객센터', '고객센타', '고객상담', 'cs', '문의', '상담', '채널톡', '카카오톡',
                     '카카오채널', '공지사항', '이용안내', '배송안내', '교환/환불', '환불정책',
                     '이용약관', '개인정보', '블로그', '유튜브', '인스타그램', '페이스북', '후기',
                     '이벤트', '공식홈페이지')
MAX_CANDIDATES = 80  # cafe.naver.com류 커뮤니티 페이지는 게시판 네비게이션까지 다 잡혀서 넘칠 수 있음
ITEM_DELAY = float(os.environ.get('ITEM_DELAY', '3'))  # 상품 사이 대기(초) — 안티봇/레이트리밋 완화
BLOCKED_STATUS_CODES = (403, 429, 490)  # 490=네이버 캡차/보안확인
BLOCKED_TEXT_MARKERS = ('security verification', '보안확인을 완료', 'unusual traffic', '비정상적인 접근')
SLOW_REDIRECT_DOMAINS = ('mkt.shopping.naver.com',)
# 검증 홉이 없어서 여기서 확정하면 그대로 DB에 들어가므로, 링크모음/스토어메인 둘 다
# 이 확신도 이상일 때만 최종 채택한다(low는 자동 확정 안 함).
LINK_PICK_OK_CONF = os.environ.get('LINK_PICK_OK_CONF', 'high,medium').split(',')

# url_type(LLM#1이 판단한 대표 구매 URL 종류)과 실제 도메인을 매칭시키는 힌트 — 후보가 여러 개일 때
# 무관한 링크를 먼저 집어서 오판하는 걸 방지 (dify_workflows/01_gonggu_classify.yml의 url_type enum과 대응)
URL_TYPE_DOMAIN_HINTS = {
    '네이버_스마트스토어': ('smartstore.naver.com', 'brand.naver.com', 'shopping.naver.com'),
    '네이버_기타': ('naver.com',),
    '쿠팡_오픈마켓': ('coupang.com', 'gmarket.co.kr', 'auction.co.kr', '11st.co.kr', 'interpark.com'),
    '카카오채널': ('kakao.com',),
}


# ---------------- LLM 호출 (판단은 전부 여기로) ----------------

def pick_link(post_context, candidates):
    """LLM#2 · 공구왕 링크선택 — 링크모음 페이지의 후보 중 하나를 고른다."""
    return call_dify({'post_context': post_context, 'candidates': candidates}, api_key=DIFY_KEY_PICK)


def judge_page(post_context, page_info):
    """LLM#3 · 공구왕 페이지판별 — 도착한 페이지가 최종 상품페이지인지 판별한다."""
    return call_dify({'post_context': post_context, 'page': page_info}, api_key=DIFY_KEY_JUDGE)


# ---------------- 크롤링/파싱 (순수 스크립트, 판단 없음) ----------------

def meta(page, prop):
    try:
        el = page.query_selector(f'meta[property="{prop}"]') or page.query_selector(f'meta[name="{prop}"]')
        return el.get_attribute('content') if el else None
    except Exception:
        return None


def extract_jsonld(html):
    out = {}
    for m in re.finditer(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, re.S):
        try:
            data = json.loads(m.group(1).strip())
        except Exception:
            continue
        items = data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])
        if isinstance(data, dict) and '@graph' in data:
            items = data['@graph']
        for it in items:
            if isinstance(it, dict):
                t = it.get('@type', '')
                t = t if isinstance(t, str) else ','.join(t)
                if 'Product' in t:
                    img = it.get('image')
                    offers = it.get('offers') or {}
                    if isinstance(offers, list):
                        offers = offers[0] if offers else {}
                    return {'name': it.get('name'), 'image': img[0] if isinstance(img, list) else img,
                            'price': offers.get('price'), 'currency': offers.get('priceCurrency')}
    return out


def _extract_once(page):
    title = meta(page, 'og:title') or (page.title() or '').strip()
    html = page.content()
    og_image = meta(page, 'og:image')
    jsonld = extract_jsonld(html)
    try:
        # 가격·구성이 JSON-LD가 아니라 본문 텍스트 중간에 있는 경우가 많아(예: "정가 238,000 공구가
        # 166,600") 2000자로 넉넉히 잡아서 LLM#3 판별 근거로 삼는다.
        body_text = page.inner_text('body')[:2000].replace('\n', ' ')
    except Exception:
        body_text = ''
    return title, og_image, jsonld, body_text


def host_of(url):
    try:
        return urlparse(url).netloc
    except Exception:
        return ''


def fetch(page, url, wait_extra=1.5):
    rec = {'status': None, 'final_url': None, 'title': None, 'og_image': None, 'jsonld': {},
           'body_text': '', 'error': None}
    try:
        resp = page.goto(url, wait_until='domcontentloaded', timeout=25000)
        try:
            page.wait_for_load_state('networkidle', timeout=6000)
        except Exception:
            pass
        time.sleep(wait_extra)
        rec['status'] = resp.status if resp else None
        rec['final_url'] = page.url

        # 네이버 마케팅 단축링크류는 클라이언트 사이드 리다이렉트가 늦게 끝나는 경우가 있음
        if host_of(rec['final_url']) in SLOW_REDIRECT_DOMAINS:
            time.sleep(3)
            try:
                page.wait_for_load_state('networkidle', timeout=4000)
            except Exception:
                pass
            rec['final_url'] = page.url

        # blog.naver.com(PC)은 본문이 iframe 안에 있어 본문 텍스트/링크 추출이 전부 0으로 나옴 —
        # 모바일(m.blog.naver.com)은 iframe 없이 직접 렌더링하니 도착지가 PC 블로그면 다시 이동.
        if host_of(rec['final_url']) == 'blog.naver.com':
            mobile_url = re.sub(r'^https?://blog\.naver\.com/', 'https://m.blog.naver.com/', rec['final_url'])
            page.goto(mobile_url, wait_until='domcontentloaded', timeout=25000)
            try:
                page.wait_for_load_state('networkidle', timeout=6000)
            except Exception:
                pass
            time.sleep(wait_extra)
            rec['final_url'] = page.url

        title, og_image, jsonld, body_text = _extract_once(page)
        if not jsonld.get('image') and not og_image:
            time.sleep(2)
            title, og_image, jsonld, body_text = _extract_once(page)

        rec['title'], rec['og_image'], rec['jsonld'], rec['body_text'] = title, og_image, jsonld, body_text
    except Exception as e:
        rec['error'] = str(e)[:160]
    return rec


def _follow_redirect(page, url, referer):
    """판단(LLM) 없이 그냥 한 번 더 열어서 진짜 목적지 URL만 얻는다 — "이 상품이 맞는지"는 안
    보고 "이 링크가 실제로 어딘가로 연결되는지"만 확인. referer를 원래 있던 페이지로 지정해야
    하는 이유는 위 호출부 주석 참고. 리다이렉트가 전혀 안 일어나면(그대로 같은 URL) None."""
    try:
        page.goto(url, referer=referer, wait_until='domcontentloaded', timeout=20000)
        try:
            page.wait_for_load_state('networkidle', timeout=6000)
        except Exception:
            pass
        final_url = page.url
    except Exception:
        return None
    return final_url if final_url.split('#')[0] != url.split('#')[0] else None


def extract_collection_links(page):
    try:
        raw = page.eval_on_selector_all(
            'a[href]', "els => els.map(e => ({href: e.href, text: e.innerText.trim()}))")
    except Exception:
        return []
    # 같은 페이지 안의 앵커/네비게이션 링크(fragment만 다르거나 완전히 같은 URL)는 실제 이동이
    # 아니니 후보에서 뺀다 — LLM#2가 이런 걸 최종 링크로 잘못 고르는 걸 방지.
    current_no_frag = page.url.split('#')[0]
    out, seen = [], set()
    for l in raw:
        href, text = l.get('href', ''), l.get('text', '')
        if not href or href in seen or any(d in href for d in BAD_DOMAINS):
            continue
        if re.match(r'^(javascript|mailto|tel):', href, re.I):
            continue
        if href.split('#')[0] == current_no_frag:
            continue
        text_norm = re.sub(r'\s+', '', text or '').lower()
        if text_norm and any(kw in text_norm for kw in NON_PRODUCT_TEXT):
            continue
        seen.add(href)
        out.append({'href': href, 'text': text})
        if len(out) >= MAX_CANDIDATES:
            break
    return out


def normalize_url(u):
    """캡션 원문에서 그대로 뽑힌 URL이라 콜론 빠짐(https//...)이나 스킴 없음, 중복 스킴 같은
    오타가 섞여 있을 수 있어 fetch 전에 보정한다."""
    u = (u or '').strip()
    if not u:
        return u
    u = re.sub(r'(https?)//', r'\1://', u)
    matches = list(re.finditer(r'https?://', u))
    if len(matches) > 1:
        u = u[matches[-1].start():]
    if not re.match(r'^https?://', u):
        u = 'https://' + u
    u = re.sub(r'^https?://blog\.naver\.com/', 'https://m.blog.naver.com/', u)
    return u


def ordered_candidates(urls, url_type=None):
    """후보 URL들을 시도할 순서대로 정렬한다 — url_type과 도메인이 일치하는 후보를 먼저,
    나머지는 원래 순서 그대로 뒤에 붙인다. "..."로 잘린 링크(캡션 원본부터 잘려서 우리가
    고칠 방법이 없는 것)는 애초에 열어볼 수 없으니 제외한다."""
    urls = [u for u in (urls or []) if u and '...' not in u]
    hints = URL_TYPE_DOMAIN_HINTS.get(url_type)
    if not hints:
        return urls
    matching = [u for u in urls if any(h in u for h in hints)]
    rest = [u for u in urls if u not in matching]
    return matching + rest


def hint_is_vague(name):
    """product_name이 "OO마켓 상품"/"OO샵 신상품"처럼 특정 상품명이 아니라 스토어명+일반명사뿐이면,
    스토어메인의 카탈로그를 거쳐 고른 아무 상품이나 "일치"로 통과시켜버릴 위험이 있다 — 이런 경우는
    done으로 자동 확정하지 않고 사람이 보게 hold로 돌린다."""
    h = (name or '').strip()
    return bool(re.match(r'^\S+\s*(마켓|샵|스토어|몰|숍)\s*(상품|제품|아이템)$', h))


def post_context_text(product, parent):
    parts = [product.get('product_name') or '']
    note = parent.get('classification_note')
    if note:
        parts.append(f'(참고: {note})')
    return ' '.join(p for p in parts if p)


def product_key(platform, parent, sort_order):
    native_id = parent.get('post_id') if platform == 'ig' else parent.get('video_id')
    return f'{platform}:{native_id}:{sort_order}'


# ---------------- 오케스트레이션 ----------------

# _resolve_one_candidate()의 결과 status를 "이 정도면 최종으로 쓸 만한가" 순으로 매긴 순위.
# 후보를 여러 개 시도했는데 전부 done이 아니면, 그중 가장 나은 상태를 대표 결과로 남긴다
# (hold: 사람이 볼 근거는 있음 > unresolved: 못 찾음 > error: 크롤링/LLM 호출 자체가 실패).
_STATUS_RANK = {'error': 0, 'unresolved': 1, 'hold': 2}


def resolve_product(page, platform, parent, product):
    """candidate_url의 후보들을 순서대로 하나씩 시도하다가 처음 done이 나오면 즉시 반환한다.
    전부 실패하면 그중 가장 나은 상태를 반환. 반환: {status, final_url, note, tried_urls}
    (tried_urls는 실제로 시도한 URL 목록 — 나중에 "어떤 링크를 열어봤는지" 진단용)."""
    raw_urls = [u for u in (product.get('candidate_url') or '').split(';') if u]
    if not raw_urls:
        return {'status': 'unresolved', 'final_url': None, 'note': '크롤링할 후보 링크 없음', 'tried_urls': []}

    candidates = ordered_candidates(raw_urls, product.get('url_type'))
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
        try:
            pick = pick_link(ctx, links)
        except Exception as e:
            return {'status': 'error', 'final_url': None, 'note': f'LLM#2 호출 실패: {str(e)[:120]}'}
        idx, confidence = pick.get('chosen_index', -1), pick.get('confidence')
        if idx is None or idx < 0 or idx >= len(links):
            return {'status': 'unresolved', 'final_url': None, 'note': 'LLM#2가 적합한 링크를 못 찾음'}
        # 검증 홉이 없어진 뒤로는 여기서 확정하면 그대로 DB에 들어간다 — 예전엔 링크모음은
        # 확신도 무관하게 최선의 후보를 채택해도 LLM#3 재검증이 저확신 오판을 걸러줬지만, 이제는
        # 그 안전망이 없으므로 링크모음/스토어메인 둘 다 확신도가 낮으면(low) 자동 확정하지 않는다.
        if confidence not in LINK_PICK_OK_CONF:
            return {'status': 'unresolved', 'final_url': None,
                    'note': f'{page_type} 후보 중 확신도 낮음(conf={confidence}) — 검증 홉이 없어서 오탐 방지로 채택 안 함'}
        chosen_href = normalize_url(links[idx]['href'])
        # ⚠ "이 링크가 맞는 상품인지" 재검증(LLM#3)은 안 하지만, "이 링크가 실제로 열리는지"는
        # 확인해야 한다 — inpock 등 링크모음 서비스의 버튼 href가 자기네 내부 리다이렉트 API
        # (예: link.inpock.co.kr/api/r/<토큰>)를 가리키는 경우가 많은데, 이 URL을 referer 없이
        # 단독으로 열면 400이 나서 아예 안 열리는 죽은 링크가 된다(실측 확인, 2026-07-16) — 지금
        # 있던 페이지에서 온 것처럼 referer를 붙여서 한 번 더 열면(판단 없는 단순 리다이렉트
        # 추적) 정상적으로 진짜 목적지로 넘어간다.
        chosen_url = _follow_redirect(page, chosen_href, referer=r['final_url'] or current_url)
        if not chosen_url:
            return {'status': 'unresolved', 'final_url': None,
                    'note': f'{page_type} 후보(conf={confidence})를 선택했지만 실제 목적지로 리다이렉트되지 않음'
                            f' — {chosen_href[:150]}'}
        # hint_is_vague는 그대로 적용해서, 상품명이 너무 일반적인 경우(스토어메인 카탈로그에서
        # 뽑은 임의의 상품일 위험)는 자동 확정하지 않고 사람 검토로 돌린다.
        if hint_is_vague(product.get('product_name')):
            return {'status': 'hold', 'final_url': chosen_url,
                    'note': f"상품명(\"{product.get('product_name')}\")이 너무 일반적이라 LLM#2 선택을"
                            f" 자동으로 확정할 수 없음 — 사람 검토 필요"}
        return {'status': 'done', 'final_url': chosen_url,
                'note': f"LLM#2 선택 채택(conf={confidence}): {(pick.get('reason') or '')[:150]}"}

    if page_type == '무관':
        # "무관"으로 판정된 것 중 일부는 명칭이 달라서 못 알아본 케이스일 수 있어 자동 실패
        # 종료 대신 사람이 검토할 "보류"로 뺀다.
        return {'status': 'hold', 'final_url': None, 'note': f"무관 — {(verdict.get('reason') or '')[:150]}"}

    # 로그인월_차단 / (상품페이지인데 원본과 불일치)
    return {'status': 'unresolved', 'final_url': None,
            'note': f"{page_type} — {(verdict.get('reason') or '')[:150]}"}


# ---------------- 실행 ----------------

def load_resolutions():
    return load_json(RESOLUTION_FILE) if RESOLUTION_FILE.exists() else {}


def build_resolved_file(items, resolutions):
    out = []
    for item in items:
        platform, parent = item['platform'], item['parent']
        new_products = []
        for p in item['products']:
            key = product_key(platform, parent, p['sort_order'])
            res = resolutions.get(key)
            np = dict(p)
            if res and res.get('status') == 'done' and res.get('final_url'):
                np['candidate_url'] = res['final_url'][:500]
            new_products.append(np)
        out.append({**item, 'products': new_products})
    dump_json(RESOLVED_FILE, out)


def main():
    if not DIFY_KEY_PICK or not DIFY_KEY_JUDGE:
        print('.env에 DIFY_KEY_PICK / DIFY_KEY_JUDGE가 필요합니다.', file=sys.stderr)
        sys.exit(1)

    items = load_json(LOAD_READY_FILE)
    resolutions = load_resolutions()

    pending = [
        (product_key(item['platform'], item['parent'], p['sort_order']), item, p)
        for item in items for p in item['products']
    ]
    pending = [(k, item, p) for k, item, p in pending if k not in resolutions]
    if len(sys.argv) > 1:
        pending = pending[:int(sys.argv[1])]

    print(f'해석 대상 {len(pending)}건 (이미 처리됨 {len(resolutions)}건)')

    if pending:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=['--disable-blink-features=AutomationControlled'])
            ctx_kwargs = dict(user_agent=UA, locale='ko-KR', viewport={'width': 1360, 'height': 900},
                               extra_http_headers={'Accept-Language': 'ko-KR,ko;q=0.9,en;q=0.8'})
            if AUTH_STATE_FILE.exists():
                ctx_kwargs['storage_state'] = str(AUTH_STATE_FILE)
            ctx = browser.new_context(**ctx_kwargs)
            # 기본값이 Win32/en-US라 UA(Mac)·locale(ko-KR)이랑 안 맞으면 오히려 더 튀어서 맞춰준다.
            Stealth(navigator_platform_override='MacIntel',
                    navigator_languages_override=('ko-KR', 'ko')).apply_stealth_sync(ctx)
            page = ctx.new_page()

            for i, (key, item, p) in enumerate(pending, 1):
                try:
                    res = resolve_product(page, item['platform'], item['parent'], p)
                except Exception as e:
                    res = {'status': 'error', 'final_url': None, 'note': str(e)[:160]}
                resolutions[key] = res
                shown = res.get('final_url') or res.get('note', '')
                print(f'  [{i}/{len(pending)}] {key} -> {res["status"]} {shown[:70]}')
                if i % 10 == 0:
                    dump_json(RESOLUTION_FILE, resolutions)
                time.sleep(ITEM_DELAY)

            dump_json(RESOLUTION_FILE, resolutions)
            AUTH_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            ctx.storage_state(path=str(AUTH_STATE_FILE))
            browser.close()

    build_resolved_file(items, resolutions)
    by_status = {}
    for r in resolutions.values():
        by_status[r['status']] = by_status.get(r['status'], 0) + 1
    print(f'누적 {len(resolutions)}건 — {by_status} -> {RESOLVED_FILE}')


if __name__ == '__main__':
    main()
