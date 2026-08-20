#!/usr/bin/env python3
"""이미 크롤된 인포크 데이터(data/linkbio/)에서 이메일을 찾아 CSV로 뽑는 일회성 스크립트 —
재크롤 없이 저장된 파싱 결과만 훑는다.

포스트 단위(data/linkbio/<게시일>.jsonl)로 순회하는 이유: poster_username(그 포스트를 올린
계정의 실제 인스타그램 핸들 — gonggu_post.user_id, crawl_linkbio.py가 채워줌)과, 그 포스트가
링크한 허브에서 찾은 이메일을 짝지어야 "유저명 vs 이메일 유저명이 얼마나 같은지" 비율을 낼 수
있다. 허브 캐시(_hub_cache.jsonl)만 보면 허브가 누구 것인지 모른다 — 인포크 자체 sns 항목에
인스타그램을 등록해둔 경우에만 알 수 있는데, 그건 크리에이터가 선택적으로 채우는 값이라
안 채운 계정은 실제로는 핸들을 알면서도 모른다고 나오는 문제가 있었다(대공사 이전 버전).

이메일은 bio/notice 자유텍스트에 섞여 있거나, sns 항목의 value(원래 인스타그램/블로그 핸들이
들어갈 자리인데 이메일을 적어둔 경우)에 들어있다. 실제 탐색 로직(url/image/resolved_url 같은
링크 필드 제외)은 linkbio_parser.extract_emails를 그대로 쓴다 — resolve_links의 일일
파이프라인(runner._dump_linkbio)이 앞으로 매일 새로 곁다리로 뽑는 것과 같은 로직이다.
이 스크립트는 그 자동화 이전에 이미 쌓여 있던 data/linkbio/ 백로그를 소급 정리할 때만 쓴다.

출력:
  - data/output/inpock_emails.csv — poster_username(실제 IG 핸들, ig 포스트만) 1개당 1행,
    이메일이 여러 개 나오면 쉼표로 이어붙인다(2026-08-11부터 세미콜론 대신 통일). inpocksns에
    등록해둔 인스타그램 핸들도 참고용으로 별도 열에 남긴다(poster_username과 다르면 인포크에
    다른 계정을 등록해뒀거나 오탈자가 있다는 뜻) — 사람이 눈으로 확인하는 용도.
  - data/output/hifen_emails.jsonl(=common.HIFEN_EMAIL_FILE) — resolve_links가 매일 새로
    채우는 것과 같은 형식으로 여기서 찾은 이메일도 함께 append한다(2026-08-11 추가). 그래야
    데일리 자동화가 생기기 전에 이미 쌓여 있던 백로그도 `python3 -m gonggu.sync_hifen_emails`
    한 번으로 hifen DB에 반영할 수 있다.

사용법(저장소 루트에서): python3 -m gonggu._extract_inpock_emails
"""
import csv
import glob
import json

from gonggu.common import HIFEN_EMAIL_FILE, ROOT, append_jsonl
from gonggu.linkbio_parser import extract_emails as _find_emails

LINKBIO_DIR = ROOT / 'data/linkbio'
HUB_CACHE_FILE = LINKBIO_DIR / '_hub_cache.jsonl'
OUT_FILE = ROOT / 'data/output/inpock_emails.csv'


def _inpock_sns_instagram(parsed):
    """인포크 sns 목록에 크리에이터가 직접 등록해둔 인스타그램 핸들(있으면). 참고용."""
    for sns in parsed.get('sns') or []:
        if sns.get('type') == 'instagram' and sns.get('value'):
            return sns['value']
    return ''


def _load_hub_cache():
    cache = {}
    with open(HUB_CACHE_FILE, encoding='utf-8') as fh:
        for line in fh:
            rec = json.loads(line)
            cache[rec['url']] = rec
    return cache


def main():
    cache = _load_hub_cache()

    # poster_username(실제 IG 핸들) -> {emails: set, inpock_handles: set}
    by_user = {}
    for path in sorted(glob.glob(str(LINKBIO_DIR / '*.jsonl'))):
        if '/_' in path:
            continue
        with open(path, encoding='utf-8') as fh:
            for line in fh:
                rec = json.loads(line)
                username = rec.get('poster_username')
                if not username:  # yt 포스트는 인스타그램 계정이 없어 대상 아님
                    continue
                for hub_url in rec.get('hub_urls', []):
                    hub = cache.get(hub_url)
                    parsed = hub.get('parsed') if hub else None
                    if not parsed:
                        continue
                    emails = _find_emails(parsed)
                    if not emails:
                        continue
                    entry = by_user.setdefault(username, {'emails': set(), 'inpock_handles': set()})
                    entry['emails'].update(emails)
                    inpock_handle = _inpock_sns_instagram(parsed)
                    if inpock_handle:
                        entry['inpock_handles'].add(inpock_handle)

    rows = [
        (username, ','.join(sorted(v['emails'])), '; '.join(sorted(v['inpock_handles'])))
        for username, v in sorted(by_user.items())
    ]

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_FILE, 'w', newline='', encoding='utf-8-sig') as fh:
        writer = csv.writer(fh)
        writer.writerow(['poster_username', 'email', 'inpock_sns_instagram_handle'])
        writer.writerows(rows)

    n_match = sum(1 for r in rows if r[0] and r[0] in r[2].split('; '))
    print(f'이메일이 있는 인스타그램 계정 {len(rows)}건 → {OUT_FILE} '
          f'(인포크 sns에 등록된 핸들이 실제 유저명과 정확히 일치 {n_match}건)')

    for username, v in sorted(by_user.items()):
        if v['emails']:
            append_jsonl(HIFEN_EMAIL_FILE, {'key': username, 'user_id': username,
                                             'emails': sorted(v['emails']),
                                             'source': 'extract_inpock_emails_backfill'})
    print(f'  → {HIFEN_EMAIL_FILE}에도 {len(rows)}개 계정 기록 — hifen DB 반영은 '
          f'`python3 -m gonggu.sync_hifen_emails`로 별도 실행')


if __name__ == '__main__':
    main()
