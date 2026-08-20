#!/usr/bin/env python3
"""링크인바이오 허브 크롤 — 우리가 수집한 공구 포스트/영상의 캡션·프로필에서 링크인바이오
허브 URL(인포크/링크트리/litt.ly 등 linkbio_parser.hosts가 지원하는 플랫폼 전체, 2026-08-11부터
인포크 한정 해제)을 찾아, linkbio_parser로 파싱한 정보 전체를 게시일별 JSONL로 저장한다.

목적: 링크인바이오 허브에는 링크/스토어/상품/텍스트(bio·notice·sns) 등 구조화 정보가 들어있고, 그
안에 크리에이터 연락 이메일이 껴 있는 경우가 있다. 여기서는 이메일만 따로 뽑지 않고
linkbio_parser.parse()가 주는 정보 전체를 그대로 저장해 둔다(이메일은 그 안에 들어옴 — 나중에
필요할 때 이 저장분에서 추출, `python3 -m gonggu._extract_inpock_emails` 참고).

저장(모두 data/linkbio/ 아래):
  <게시일>.jsonl   — 레코드=포스트별 {key, platform, post_id, publish_date, hub_urls, linkbio}.
                     허브가 하나도 없는 포스트는 파일에 안 남긴다(체크포인트엔 남김).
  _processed.jsonl — 이미 스캔한 포스트 key(재실행/데일리에서 스킵 — DB 상태가 곧 증분 기준).
  _hub_cache.jsonl — 허브 URL별 파싱 결과 캐시. 같은 크리에이터 허브를 여러 포스트가 공유하므로
                     고유 허브당 1회만 크롤한다.

⚠ 증분/재실행 안전: 첫 실행은 백로그 전수, 이후(데일리) 실행은 아직 스캔 안 한 새 포스트만
처리한다. 그래서 이 모듈 하나가 "백로그 임시 크롤"과 "데일리 편입"을 겸한다.

사용법(저장소 루트에서):
    python3 -m gonggu.crawl_linkbio
    LIMIT=200 python3 -m gonggu.crawl_linkbio            # 소규모 테스트
    LINKBIO_CONCURRENCY=8 python3 -m gonggu.crawl_linkbio
    RESOLVE_INNER=1 python3 -m gonggu.crawl_linkbio      # 허브 내부 /api/r/ 링크의 최종주소까지
                                                         # 추적(파이프라인 parse와 동일, 느림)
"""
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from gonggu import linkbio_parser
from gonggu.common import ROOT, acquire_lock, append_jsonl, connect_dst, connect_src, load_jsonl

OUT_DIR = ROOT / 'data/linkbio'
PROCESSED_FILE = OUT_DIR / '_processed.jsonl'
HUB_CACHE_FILE = OUT_DIR / '_hub_cache.jsonl'
CONCURRENCY = int(os.environ.get('LINKBIO_CONCURRENCY', '8'))
RESOLVE_INNER = os.environ.get('RESOLVE_INNER', '0') == '1'
_CHUNK = 400

# 도메인/유저명 한 조각짜리 URL(예: linktr.ee/abc, link.inpock.co.kr/abc) 후보를 우선 넓게
# 잡고, 실제 링크인바이오 서비스인지는 linkbio_parser.detect_platform으로 걸러낸다 — 그래야
# hosts.py에 새 플랫폼이 추가돼도 여기를 따로 안 고쳐도 된다. group(1)=host, group(2)=유저명.
# /api/r/<토큰>(인포크 내부 버튼 리다이렉트)은 허브가 아니라 개별 상품 링크이므로 유저명 자리가
# 'api'로 잡히는 것을 아래에서 걸러낸다.
_HUB_URL_RE = re.compile(r'https?://([A-Za-z0-9.-]+)/([A-Za-z0-9._%-]+)', re.I)


def extract_linkbio_hubs(*texts):
    """여러 텍스트(캡션·프로필 URL 등)에서 linkbio_parser가 지원하는 플랫폼의 허브 URL만
    뽑아 정규화·중복제거해 돌려준다. /api/r/ 형태(버튼 리다이렉트)와 빈 유저명, 미지원 도메인은
    제외. 등장 순서를 보존한다."""
    hubs, seen = [], set()
    for t in texts:
        if not t:
            continue
        for m in _HUB_URL_RE.finditer(t):
            host, user = m.group(1).lower(), m.group(2)
            if user.lower() == 'api':          # /api/r/... 는 허브 아님
                continue
            hub = f'https://{host}/{user}'
            try:
                linkbio_parser.detect_platform(hub)
            except ValueError:                 # 링크인바이오 서비스가 아닌 일반 도메인
                continue
            if hub not in seen:
                seen.add(hub)
                hubs.append(hub)
    return hubs


def _fetch_posts(dst):
    """우리 DB(dev_gongguking)의 공구 포스트/영상 — (code, native_id, publish_date, profile_ctx).
    profile_ctx: ig=user_id(프로필 외부링크 조회용), yt=채널 external_url(그 자체가 링크)."""
    posts = []
    with dst.cursor() as cur:
        cur.execute('SELECT post_id, user_id, publish_date FROM gonggu_post')
        for r in cur.fetchall():
            posts.append(('ig', r['post_id'], _date(r['publish_date']), r['user_id']))
        cur.execute('SELECT video_id, external_url, publishDate FROM gonggu_video')
        for r in cur.fetchall():
            posts.append(('yt', r['video_id'], _date(r['publishDate']), r['external_url']))
    return posts


def _date(v):
    s = str(v)[:10] if v is not None else ''
    return s if re.fullmatch(r'\d{4}-\d{2}-\d{2}', s or '') else 'unknown'


def _fetch_captions(src, posts):
    """hifen에서 캡션 배치 조회 → {(code, native_id): caption}."""
    ids = {'ig': set(), 'yt': set()}
    for code, nid, _, _ in posts:
        ids[code].add(nid)
    sql = {
        'ig': 'SELECT post_id AS k, description AS caption FROM instagram_post_description WHERE post_id IN ({ph})',
        'yt': 'SELECT video_id AS k, video_description AS caption FROM YT_video_lists_detail WHERE video_id IN ({ph})',
    }
    out = {}
    with src.cursor() as cur:
        for code in ('ig', 'yt'):
            id_list = sorted(ids[code])
            for i in range(0, len(id_list), _CHUNK):
                chunk = id_list[i:i + _CHUNK]
                if not chunk:
                    continue
                ph = ', '.join(['%s'] * len(chunk))
                cur.execute(sql[code].format(ph=ph), chunk)
                for r in cur.fetchall():
                    out[(code, r['k'])] = r['caption'] or ''
    return out


def _fetch_ig_bios(src, user_ids):
    """instagram_user_external_url — user_id별 프로필 외부링크(여러 개면 공백 join)."""
    id_list = sorted({u for u in user_ids if u})
    if not id_list:
        return {}
    acc = {}
    with src.cursor() as cur:
        for i in range(0, len(id_list), _CHUNK):
            chunk = id_list[i:i + _CHUNK]
            ph = ', '.join(['%s'] * len(chunk))
            cur.execute(f'SELECT user_id AS k, external_url FROM instagram_user_external_url '
                        f'WHERE user_id IN ({ph})', chunk)
            for r in cur.fetchall():
                acc.setdefault(r['k'], []).append(r['external_url'] or '')
    return {k: ' '.join(v) for k, v in acc.items()}


def main():
    acquire_lock('crawl_linkbio')
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    processed = set(load_jsonl(PROCESSED_FILE))

    dst = connect_dst()
    try:
        posts = _fetch_posts(dst)
    finally:
        dst.close()

    todo = [p for p in posts if f'{p[0]}:{p[1]}' not in processed]
    remaining = len(todo)
    limit = int(os.environ.get('LIMIT', '0')) or remaining
    todo = todo[:limit]
    print(f'공구 포스트/영상 {len(posts)}개 중 미처리 {remaining}개 → 이번 실행 {len(todo)}개'
          f'{f" (LIMIT으로 {remaining - len(todo)}개 보류)" if len(todo) < remaining else ""}')
    if not todo:
        print('  새로 스캔할 포스트가 없습니다.')
        return

    src = connect_src()
    try:
        captions = _fetch_captions(src, todo)
        bios = _fetch_ig_bios(src, [ctx for code, _, _, ctx in todo if code == 'ig'])
    finally:
        src.close()

    # 포스트별 링크인바이오 허브 추출(캡션 + 프로필/채널 링크)
    per_post, all_hubs = [], set()
    for code, nid, date, ctx in todo:
        cap = captions.get((code, nid), '')
        bio = bios.get(ctx, '') if code == 'ig' else (ctx or '')  # yt: 채널 external_url 그 자체
        hubs = extract_linkbio_hubs(cap, bio)
        per_post.append((f'{code}:{nid}', code, nid, date, hubs, ctx))
        all_hubs.update(hubs)
    n_with_hub = sum(1 for r in per_post if r[4])
    print(f'  링크인바이오 허브: 고유 {len(all_hubs)}개, 허브 있는 포스트 {n_with_hub}개'
          f'{" (RESOLVE_INNER=1: 내부 링크까지 추적)" if RESOLVE_INNER else ""}')

    # 고유 허브 크롤(캐시) — 허브당 1회만
    cache = load_jsonl(HUB_CACHE_FILE)
    to_crawl = [h for h in all_hubs if h not in cache]
    print(f'  허브 크롤 대상 {len(to_crawl)}개 (캐시 재사용 {len(all_hubs) - len(to_crawl)}개), 동시 {CONCURRENCY}')

    def crawl(hub):
        try:
            return {'key': hub, 'url': hub, 'parsed': linkbio_parser.parse(hub, resolve_links=RESOLVE_INNER),
                    'error': None}
        except Exception as e:
            return {'key': hub, 'url': hub, 'parsed': None, 'error': str(e)[:200]}

    if to_crawl:
        with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
            futures = [ex.submit(crawl, h) for h in to_crawl]
            for i, fut in enumerate(as_completed(futures), 1):
                rec = fut.result()
                cache[rec['url']] = rec
                append_jsonl(HUB_CACHE_FILE, rec)
                if i % 50 == 0 or i == len(to_crawl):
                    ok = sum(1 for h in to_crawl if cache.get(h) and not cache[h].get('error'))
                    print(f'    허브 {i}/{len(to_crawl)} 크롤 (성공 {ok})')

    # 포스트별 레코드 저장(게시일 샤딩) + 처리 체크포인트
    written = 0
    for key, code, nid, date, hubs, ctx in per_post:
        if hubs:
            linkbio = [{'hub_url': h, 'parsed': (cache.get(h) or {}).get('parsed'),
                        'error': (cache.get(h) or {}).get('error')} for h in hubs]
            rec = {'key': key, 'platform': code, 'post_id': nid, 'publish_date': date,
                   'hub_urls': hubs, 'linkbio': linkbio}
            if code == 'ig':
                # 실제 인스타그램 핸들(gonggu_post.user_id) — _extract_inpock_emails.py가
                # 이메일을 계정과 짝짓는 데 쓴다.
                rec['poster_username'] = ctx
            append_jsonl(OUT_DIR / f'{date}.jsonl', rec)
            written += 1
        append_jsonl(PROCESSED_FILE, {'key': key})

    crawl_ok = sum(1 for h in all_hubs if cache.get(h) and not cache[h].get('error'))
    print(f'완료 — 포스트 {len(per_post)}개 스캔, 허브 보유 {written}개를 data/linkbio/<게시일>.jsonl에 저장 '
          f'(고유 허브 {len(all_hubs)}개 중 파싱 성공 {crawl_ok}개)')


if __name__ == '__main__':
    main()
