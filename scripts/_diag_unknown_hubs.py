#!/usr/bin/env python3
"""진단 — "LLM#3가 링크모음이라 했는데 우리 목록엔 없는" 호스트를 체크포인트 이력에서 캔다.

배경(2026-08-19): 링크모음 판별은 두 층이다. 1층은 도메인 대조(공짜) — linkbio_parser가
지원하면 브라우저 없이 구조화 데이터로 끝나고, config.KNOWN_HUB_HOSTS면 브라우저는 쓰되
LLM#3 홉을 건너뛴다. 2층은 실제로 페이지를 열어 LLM#3에게 "이 페이지 뭐야?"를 묻는 것(비쌈).

몇 주 돌리다 보면 2층에서 "링크모음"이라 판정된 새 서비스가 계속 쌓이는데, 그게 1층 목록에
반영되지 않으면 같은 도메인을 매번 비싼 길로 보낸다. 실제로 첫 조사에서 미등록 호스트가
129종·752회 나왔다. 이 스크립트가 그 목록을 언제든 다시 뽑아준다 — 목록 갱신을 감이 아니라
데이터로 하기 위한 도구다.

읽는 법:
- 등장이 많고 실패가 적은 서비스 → KNOWN_HUB_HOSTS에 추가할 후보(DOM 추출은 잘 되니
  LLM#3 홉만 아끼면 된다).
- 등장이 많은데 실패가 100%인 서비스 → 추가하지 말 것. 넣어봐야 브라우저만 쓰고 빈손이다
  (예: page.im은 소유자 편집 화면이 렌더링돼서 애초에 긁을 링크가 없다).
- 서브도메인 개수가 여럿인 서비스 → 등록 도메인만 넣을 것. 매칭이 접미사 기준이라 계정별
  주소는 자동으로 딸려온다.

사용법(저장소 루트에서):
    python3 -m gonggu._diag_unknown_hubs
    python3 -m gonggu._diag_unknown_hubs 40      # 상위 40개까지
"""
import collections
import json
import sys
from urllib.parse import urlparse

from gonggu.common import ROOT
from gonggu.linkbio_parser.hosts import match_host
from gonggu.resolve_links.antibot import is_excluded_marketplace, is_known_hub, is_uc_host
from gonggu.resolve_links.config import BAD_DOMAINS, NON_MALL_DOMAINS

RESOLUTION_FILE = ROOT / 'data/output/link_resolution.jsonl'


def is_definitely_not_a_hub(host):
    """LLM#3가 "링크모음"이라 불렀지만 링크모음일 리 없는 곳 — 네이버 카페/스마트스토어,
    카카오 오픈채팅·채널, 네이버 블로그, 제외 마켓플레이스. 실측(2026-08-19)에서 cafe.naver.com이
    75회, open.kakao.com이 20회 "링크모음"으로 판정됐다. 이걸 안 걸러내면 이 진단이 naver.com을
    "허브 추가 후보 1순위"로 추천하는 위험한 조언을 한다.

    ⚠ 이건 진단 표시용 분류일 뿐 실행 경로를 바꾸지 않는다 — 이 호스트들이 애초에 후보로
    들어오지 않게 막는 건 별개 작업이다(open.kakao.com/pf.kakao.com은 이미 BAD_DOMAINS인데도
    링크모음으로 열리고 있다)."""
    url = f'https://{host}/'
    return (is_uc_host(url) or is_excluded_marketplace(url)
            or host in NON_MALL_DOMAINS
            or any(d in host for d in BAD_DOMAINS))


def registrable(host):
    """`jiy1067.linkstory.co.kr` -> `linkstory.co.kr`. 완벽한 공개접미사 목록은 아니지만
    (그건 외부 의존이 필요하다) co.kr/ne.kr류 2단계 국가도메인만 처리하면 실무엔 충분하다."""
    parts = (host or '').split('.')
    if len(parts) >= 3 and parts[-2] in ('co', 'ne', 'or', 'go', 'pe'):
        return '.'.join(parts[-3:])
    return '.'.join(parts[-2:])


def collect(path=RESOLUTION_FILE):
    """반환: {등록도메인: {'n', 'fail', 'subs'}} — 이미 아는 곳(파서 보유/KNOWN_HUB_HOSTS)은 뺀다."""
    agg = collections.defaultdict(lambda: {'n': 0, 'fail': 0, 'subs': set(), 'not_hub': False})
    if not path.exists():
        return agg
    with open(path, encoding='utf-8') as f:
        for line in f:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            note = rec.get('note') or ''
            if '링크모음' not in note:
                continue
            failed = '추출 실패' in note
            for url in (rec.get('tried_urls') or []):
                host = (urlparse(url).hostname or '').lower()
                if not host or match_host(host) or is_known_hub(url):
                    continue  # 이미 파서가 있거나 KNOWN_HUB_HOSTS에 든 곳
                e = agg[registrable(host)]
                e['n'] += 1
                e['subs'].add(host)
                if is_definitely_not_a_hub(host):
                    e['not_hub'] = True
                if failed:
                    e['fail'] += 1
    return agg


def main():
    top = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    agg = collect()
    if not agg:
        print('미등록 링크모음 호스트가 없습니다 (또는 체크포인트가 비어 있습니다).')
        return
    rows = sorted(agg.items(), key=lambda kv: -kv[1]['n'])
    total_n = sum(v['n'] for v in agg.values())
    print(f'미등록 링크모음 서비스 {len(agg)}종 / 등장 총합 {total_n}회 — 상위 {min(top, len(rows))}개\n')
    print(f"  {'서비스':26s} {'등장':>6s} {'실패':>6s} {'실패율':>7s} {'서브도메인':>10s}  판단")
    for name, v in rows[:top]:
        rate = v['fail'] / v['n'] if v['n'] else 0
        if v['not_hub']:
            verdict = 'LLM#3 오분류(허브 아님) — 추가 금지'
        elif rate >= 0.9:
            verdict = '추가 금지(열어도 빈손)'
        elif v['n'] >= 20 and rate <= 0.3:
            verdict = '★ KNOWN_HUB_HOSTS 추가 후보'
        else:
            verdict = '관찰'
        print(f'  {name:26s} {v["n"]:6d} {v["fail"]:6d} {rate:6.0%} {len(v["subs"]):10d}  {verdict}')
    print('\n추가는 gonggu/resolve_links/config.py의 KNOWN_HUB_HOSTS에 **등록 도메인만** 넣으세요'
          ' (접미사 매칭이라 계정별 서브도메인은 자동으로 따라옵니다).')


if __name__ == '__main__':
    main()
