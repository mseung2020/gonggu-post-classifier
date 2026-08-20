#!/usr/bin/env python3
"""3단계: 02_classified에 "확실한 공구만 보수적으로" 게이트를 적용하고,
gonggu_video/gonggu_video_product 또는 gonggu_post/gonggu_post_product 컬럼에 그대로
매핑되는 형태로 정리한다. 크롤링/링크 최종 확정은 이 스크립트의 책임이 아님 — candidate_url은
LLM이 상품별로 뽑은 원본 후보를 그대로 세미콜론으로 이어붙여 참고용으로만 넘긴다.

실행 모드(대공사 3단계 C1, 2026-08-05):
- 기본(증분): 지난 실행 이후 **내용이 바뀐 02_classified 날짜 파일만** 다시 계산해서 그 날짜의
  03_load_ready 파일만 교체한다. 바뀌었는지는 날짜가 아니라 파일 서명(mtime+크기,
  data/output/transform_state.json에 기록)으로 판단하므로, 옛 날짜 파일에 재시도 결과가
  append된 경우도 정확히 다시 잡는다. 처리 안 한 날짜의 03 파일은 그대로 둔다 — load.py는
  이미 DB에 있는 건 스킵하고 보강 단계(6/7/9)는 DB만 보므로, 이미 적재된 날짜를 매번 다시
  계산하는 건 순수 낭비였다(02가 ~145MB로 하루 5~12MB씩 자라는데 매일 전체 재계산이었음).
- --full: 예전 동작 그대로 — 02 전체를 다시 계산하고 03을 전부 비우고 새로 쓴다.
  **게이트 규칙(이 파일의 판정 로직)을 바꿨을 때는 반드시 --full로 한 번 돌릴 것** —
  증분 모드는 "입력이 안 바뀐 날짜"를 건너뛰므로 규칙 변경이 옛 날짜에 반영되지 않는다.

사용법:
    python3 -m gonggu.transform            # 증분(권장, 일일 퀘스트가 쓰는 모드)
    python3 -m gonggu.transform --full     # 전체 재계산(규칙 변경 후 / 상태 파일이 의심될 때)
결과: data/03_load_ready/<발행일>.jsonl — 레코드 1개=1줄, {platform, parent: {...}, products: [...]}
    + 사유별 제외 건수 출력
"""
import datetime
import json
import os
import sys
from collections import Counter

from gonggu.common import (CLASSIFIED_DIR, LOAD_READY_DIR, ROOT, clear_json_dir, dump_json,
                     dump_jsonl_sharded, is_affiliate_ranking, load_json_dir, parent_date_key)

VALID_LINK_LOCATIONS = {'설명_직접링크', '설명_프로필안내', '댓글참여_DM', '고정댓글_더보기', '링크없음_불명'}

# 증분 모드의 "지난 실행 때 02 파일이 어떤 모습이었나" 기록: {날짜: [mtime_ns, size]}
STATE_FILE = ROOT / 'data/output/transform_state.json'


def _today_iso():
    """'오늘'의 단일 정의 — 평소에는 실제 오늘 날짜지만, 테스트(골든 diff)에서는 GONGGU_TODAY
    환경변수(YYYY-MM-DD)로 고정할 수 있다. 이 훅이 있어야 transform이 완전 결정론이 되어
    리팩터링 전후 산출물을 바이트 단위로 비교할 수 있다(2026-08-05 대공사 0단계에서 추가).
    운영 실행에서는 GONGGU_TODAY를 설정하지 않으므로 동작이 예전과 완전히 같다."""
    return os.environ.get('GONGGU_TODAY') or datetime.date.today().isoformat()


def _compute_stage(start, end):
    """gonggu_start_date/end_date(구조화된 날짜)를 오늘 날짜와 직접 비교해서 계산한다.
    ⚠ LLM#1이 캡션 문구만 보고 주는 gonggu_stage(예고/진행중/마감)는 "포스트 작성 시점의
    오늘"을 기준으로 판단한 힌트라 실제 지금 날짜와 어긋날 수 있어(다이파이 프롬프트에도
    이 경고가 있음) 여기서는 안 쓰고, 날짜 자체를 비교해서 결정론적으로 계산한다."""
    today = _today_iso()
    if start and start > today:
        return '시작전'
    if end and end < today:
        return '종료'
    if start or end:
        return '진행중'
    return '판단불가'


def _valid_date(s):
    """YYYY-MM-DD 형식만 신뢰. LLM이 null/이상한 값을 주면 None."""
    if not s:
        return None
    s = str(s)[:10]
    try:
        y, m, d = map(int, s.split('-'))
        assert 1 <= m <= 12 and 1 <= d <= 31
        return s
    except Exception:
        return None


def _product_row(p, sort_order, fallback_start=None, fallback_end=None):
    loc = p.get('link_location')
    if loc not in VALID_LINK_LOCATIONS:
        loc = '링크없음_불명'
    urls = [u for u in (p.get('urls') or []) if u]
    # 공구기간/스테이지는 상품(product) 단위로 이전됨(대공사 2026-08-06). 상품별 기간(신 스키마)을
    # 우선 읽고, 없으면 포스트 전체 기간(구 스키마 classification.period_*)을 폴백으로 각 상품에
    # 적용한다 — 단일상품은 정확하고, 기존 02_classified를 --full로 재계산할 때도 호환된다.
    start = _valid_date(p.get('period_start')) or fallback_start
    end = _valid_date(p.get('period_end')) or fallback_end
    return {
        'product_name': (p.get('name') or '').strip()[:300],
        'link_location': loc,
        'url_type': p.get('url_type') if p.get('url_type') and p.get('url_type') != '없음' else None,
        'candidate_url': ';'.join(urls)[:500] if urls else None,
        'sort_order': sort_order,
        'gonggu_start_date': start,
        'gonggu_end_date': end,
        'gonggu_stage': _compute_stage(start, end),
    }


def transform_one(post):
    """(parent_row, product_rows, reject_reason) 튜플. reject_reason이 있으면 제외."""
    lc = post.get('classification')
    if post.get('classification_error') or not lc:
        return None, None, f'분류실패: {post.get("classification_error") or "결과 없음"}'

    if not lc.get('is_gonggu'):
        return None, None, 'is_gonggu=false'

    raw_products = [p for p in (lc.get('products') or []) if p and (p.get('name') or '').strip()]
    if not raw_products:
        return None, None, 'products 배열 비어있음(is_gonggu=true인데 상품 특정 실패)'

    all_urls = [u for p in raw_products for u in (p.get('urls') or [])]
    if is_affiliate_ranking(post.get('description'), all_urls):
        return None, None, '제휴 광고성 다중 링크(TOP N 리뷰)'

    # 상품별 기간(신 스키마) 우선, 포스트 전체 기간(구 스키마)은 폴백으로 각 상품에 적용.
    fb_start = _valid_date(lc.get('period_start'))
    fb_end = _valid_date(lc.get('period_end'))
    product_rows = [_product_row(p, i, fb_start, fb_end) for i, p in enumerate(raw_products)]

    # 기간/스테이지는 parent가 아니라 product에 있다(완전 이전). parent에는 이 게시물이 여러
    # 공구를 나열한 예고 달력인지(is_calendar_feed)만 둔다.
    is_calendar = 1 if lc.get('is_calendar_feed') else 0
    note = (lc.get('pattern_note') or '').strip()[:500] or None

    if post['platform'] == 'ig':
        parent = {
            'post_id': post['post_id'],
            'user_id': post['user_id'],
            'url': post.get('url'),
            'publish_date': post['publish_date'],
            'is_calendar_feed': is_calendar,
            'classification_note': note,
        }
    else:
        parent = {
            'video_id': post['video_id'],
            'channel_id': post['channel_id'],
            'title': post.get('title'),
            'video_url': post.get('video_url'),
            'publishDate': post['publishDate'],
            'is_calendar_feed': is_calendar,
            'classification_note': note,
        }
    return parent, product_rows, None


def _gate_all(posts):
    """포스트 목록에 게이트를 적용 — (accepted 목록, 제외 사유 Counter)."""
    accepted = []
    reasons = Counter()
    for post in posts:
        parent, products, reject_reason = transform_one(post)
        if reject_reason:
            reasons[reject_reason.split('(')[0].split(':')[0].strip()] += 1
            continue
        accepted.append({'platform': post['platform'], 'parent': parent, 'products': products})
    return accepted, reasons


def _print_summary(n_posts, accepted, reasons, suffix=''):
    ig_n = sum(1 for a in accepted if a['platform'] == 'ig')
    yt_n = sum(1 for a in accepted if a['platform'] == 'yt')
    print(f'전체 {n_posts}건 중 확정 공구 {len(accepted)}건(ig {ig_n} / yt {yt_n}) '
          f'-> {LOAD_READY_DIR}/*.jsonl (날짜별){suffix}')
    print('제외 사유:')
    for reason, n in reasons.most_common():
        print(f'  {n:4d}  {reason}')


def _current_meta(dir_path):
    """{날짜: [mtime_ns, size]} — 파일 내용 변경 감지용 서명."""
    meta = {}
    for f in sorted(dir_path.glob('*.jsonl')):
        st = f.stat()
        meta[f.stem] = [st.st_mtime_ns, st.st_size]
    return meta


def changed_dates(meta, state):
    """서명이 지난 실행 기록과 다른(또는 처음 보는) 날짜만 골라낸다. 파일이 사라진 날짜
    (아카이브됨)는 대상에 안 잡힌다 — 그 날짜의 03 산출물은 그대로 두는 게 맞다."""
    return [d for d, sig in meta.items() if state.get(d) != sig]


def _load_jsonl_file(path):
    out = []
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def _load_state():
    if not STATE_FILE.exists():
        return {}
    try:
        return json.load(open(STATE_FILE, encoding='utf-8'))
    except Exception:
        return {}  # 상태 파일이 깨졌으면 전체를 다시 보는 쪽으로(안전한 방향)


def main():
    full = '--full' in sys.argv

    if full:
        # 예전 동작 그대로: 02 전체 재계산 + 03 전부 비우고 새로 쓰기(결정론적).
        posts = load_json_dir(CLASSIFIED_DIR)
        accepted, reasons = _gate_all(posts)
        clear_json_dir(LOAD_READY_DIR)
        dump_jsonl_sharded(LOAD_READY_DIR, accepted, parent_date_key)
        dump_json(STATE_FILE, _current_meta(CLASSIFIED_DIR), indent=None)
        _print_summary(len(posts), accepted, reasons, suffix=' [--full 전체 재계산]')
        return

    meta = _current_meta(CLASSIFIED_DIR)
    state = _load_state()
    todo_dates = changed_dates(meta, state)
    unchanged = len(meta) - len(todo_dates)
    if not todo_dates:
        print(f'02_classified 변경 없음(날짜 파일 {len(meta)}개 전부 지난 실행과 동일) — 03_load_ready 그대로 둠. '
              f'게이트 규칙을 바꿨다면 --full로 전체 재계산할 것.')
        return

    posts = []
    for d in sorted(todo_dates):
        posts.extend(_load_jsonl_file(CLASSIFIED_DIR / f'{d}.jsonl'))
    accepted, reasons = _gate_all(posts)

    # 처리한 날짜의 03 파일만 교체한다(accepted가 0건인 날짜도 빈 파일로 교체 — stale 방지).
    # 02가 발행일 기준 샤딩이므로 그 레코드들의 parent 발행일 버킷도 같은 날짜 집합이다.
    dump_jsonl_sharded(LOAD_READY_DIR, accepted, parent_date_key, only_keys=sorted(todo_dates))
    state.update({d: meta[d] for d in todo_dates})
    dump_json(STATE_FILE, state, indent=None)

    print(f'증분 모드: 변경된 날짜 {len(todo_dates)}개({", ".join(sorted(todo_dates)[:5])}'
          f'{" ..." if len(todo_dates) > 5 else ""}) 재계산, 변경 없는 날짜 {unchanged}개는 그대로 '
          f'(전체 재계산은 --full)')
    _print_summary(len(posts), accepted, reasons, suffix=' [증분 — 처리한 날짜 기준]')


if __name__ == '__main__':
    main()
