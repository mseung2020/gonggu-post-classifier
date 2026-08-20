#!/usr/bin/env python3
"""2단 재검증(uc) — resolve가 "재검증 중 차단(로그인월/캡차)"으로 포기했던 상품을 골라,
undetected_chromedriver(uc) 엔진으로 그 페이지를 실제로 열어 재검증을 다시 시도한다.

배경: bare 링크인바이오 버튼(source='link')은 과거 오탐(스토어 메인이 엉뚱한 상품으로 확정된
2026-07-21 사고) 때문에 확신도가 high여도 반드시 실제 페이지를 열어 LLM#3로 검증한다
(picker.finalize_pick의 force_verify). 그런데 네이버 스마트스토어가 로그인월/캡차로 막으면
"내용 못 본 채 확정 안 함"으로 unresolved가 됐다 — 링크가 틀려서가 아니라 검증을 못 해서 버린
것. enrich_detail에서 네이버를 뚫는 그 uc 엔진(gonggu.uc_engine)을 resolve의 재검증 크롤링에도
붙여(RESOLVE_UC=1) 진짜로 열어보고 판정한다. 차단을 *우회*해 무조건 통과시키는 게 아니라, 막혀서
못 하던 검증을 *작동*시키는 것 — 오판 방지 안전장치는 그대로 살아있다(uc로 연 페이지도 LLM#3
재검증을 통과해야 done).

대상: link_status='unresolved' 이고 link_note가 차단 계열인 상품(2026-08-07 추가한 link_note
컬럼이 그대로 작업 큐가 된다). gonggu_stage는 안 따진다 — 공구가 끝났어도 링크 자산은 enrich에
쓰이므로 확정할 가치가 있다.

⚠ 대상 확대(2026-08-19) — 예전엔 note에 '재검증 중 차단'이 있는 것만 골랐는데, 실측해보니 그
큐는 4건뿐이었고 정작 uc가 뚫으라고 만들어진 네이버 안티봇 건들은 '로그인월_차단'이라는 다른
문구로 988건이 쌓여 있었다(호스트 상위: m.smartstore.naver.com 134, smartstore.naver.com 124,
naver.me 27). 문구 하나 차이로 uc 패스가 사실상 놀고 있던 셈이라 두 문구를 모두 대상으로 넓혔다.
넓히면서 낭비를 막는 안전판 셋을 같이 둔다 — 이 패스는 사람이 곁에 앉아 있는 비싼 패스라
"열어봐야 소용없는 건"을 큐에 넣지 않는 게 자동 패스보다 훨씬 중요하다:
  1) 죽은 페이지/정책 제외 노트는 뺀다(DEAD_NOTE_MARKERS — 404·존재하지 않음·오류 페이지·
     인스타·알리 등 992 → 801). uc로 창을 띄워도 절대 안 풀린다.
  2) uc가 실제로 도움 되는 건만 남긴다(_is_uc_addressable — RESOLVE_UC_HOSTS에 걸리는 호스트,
     또는 노트가 안티봇/보안확인을 가리키는 경우. 801 → 698). 자사몰 403은 Playwright로도
     같은 결과라 uc를 띄울 이유가 없다.
  3) 시도 이력과 은퇴(UC_STATE_FILE + UC_MAX_ATTEMPTS). 예전엔 4건짜리 큐라 없어도 무해했지만,
     700건 규모에서 은퇴가 없으면 uc로도 안 뚫리는 건들이 매 실행마다 사람 시간을 갉아먹는다
     — rescan_inprogress의 rescan_state.jsonl + RESCAN_ERROR_MAX_ATTEMPTS와 같은 방식.

⚠ 이 패스만 uc를 켠다(모듈 상단에서 RESOLVE_UC=1 자동 설정) — 본 resolve/대량 경로는 그대로.
uc는 실제 크롬 창 + (필요 시) 사람이 캡차 통과라, enrich stage 2처럼 사람이 곁에 있을 때 낮은
동시성으로 돌린다. 먼저 전용 프로필에 네이버 신뢰를 쌓아둘 것:
    python3 -m gonggu.enrich_detail.warmup_naver_uc

DB 상태가 곧 체크포인트다(idempotent) — done이 되면 대상에서 빠지고, uc가 못 뚫은 건 여전히
unresolved(note 갱신)로 남아 다음 실행에 다시 잡힌다.

사용법(저장소 루트에서):
    python3 -m gonggu.resolve_links.reverify_uc
    LIMIT=20 python3 -m gonggu.resolve_links.reverify_uc
    REVERIFY_CONCURRENCY=1 python3 -m gonggu.resolve_links.reverify_uc
    RESOLVE_UC_HOSTS=naver.,gmarket.co.kr python3 -m gonggu.resolve_links.reverify_uc  # 오픈마켓도
    UC_MAX_ATTEMPTS=5 python3 -m gonggu.resolve_links.reverify_uc   # 은퇴 상한 조정(기본 3)
    UC_TIME_BUDGET_SEC=1800 python3 -m gonggu.resolve_links.reverify_uc  # 30분만 갉고 정상 종료

⚠ 이 단계는 큐를 비우는 단계가 아니라 **매일 조금씩 갉는 단계**다 — 유입(하루 약 436건)이 배수
(30분 예산에 24~120건)보다 크다. 근거와 숫자는 아래 UC_TIME_BUDGET_SEC 주석에 있다.
"""
import os

# uc 폴백을 이 패스 한정으로 강제한다 — browser.fetch가 차단 시 uc로 재시도하도록. 다른
# import보다 먼저 세팅해야(browser._uc_enabled_for는 호출 시점에 읽지만, 명시적으로 앞에 둔다).
os.environ.setdefault('RESOLVE_UC', '1')
# 차단으로 포기한 건의 대부분이 네이버 로그인월이 아니라 오픈마켓 403(G마켓·옥션·오늘의집·
# 11번가)이라(2026-08-07 진단), uc 대상 호스트 기본값을 이 블로커들로 넓힌다. 특정 몰만 하고
# 싶으면 RESOLVE_UC_HOSTS로 덮어쓰면 된다(예: 네이버만 → RESOLVE_UC_HOSTS=naver.).
# ⚠ 쿠팡/알리/테무는 정책상 공구 대상에서 제외(config.EXCLUDED_MARKETPLACE_DOMAINS)라 여기서도
# uc 대상에 넣지 않는다 — resolve가 애초에 후보로 안 쓰므로 reverify 대상으로도 안 잡힌다(2026-08-11).
os.environ.setdefault('RESOLVE_UC_HOSTS',
                      'naver.,gmarket.co.kr,auction.co.kr,ohou.se,11st.co.kr')

import datetime  # noqa: E402
import json  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
from collections import Counter  # noqa: E402

from gonggu.common import (  # noqa: E402
    DEEPSEEK_KEY, ROOT, acquire_lock, append_jsonl, connect_dst, load_jsonl)
from gonggu.crawl_pool import run_crawl_pool  # noqa: E402
from gonggu.platforms import PLATFORMS, parent_ctx_from_row, product_update_link_sql  # noqa: E402
from gonggu.resolve_links.browser import _uc_enabled_for  # noqa: E402
from gonggu.resolve_links.config import ITEM_DELAY, ITEM_DELAY_SMART, MAX_BROWSERS  # noqa: E402
from gonggu.resolve_links.core import resolve_product  # noqa: E402
from gonggu.resolve_links.matching import product_key  # noqa: E402

CONCURRENCY = int(os.environ.get('REVERIFY_CONCURRENCY', '2'))
# 차단 계열 note 두 종류(2026-08-19 확대, 모듈 docstring 참고). '재검증 중 차단'은 fast resolve가
# uc 대상 호스트라 브라우저를 아예 생략하고 넘긴 것(4건), '로그인월_차단'은 실제로 열었다가 막힌
# 것(988건) — 후자가 압도적인데 예전 쿼리는 전자만 봤다.
BLOCKED_NOTE_LIKES = ('%재검증 중 차단%', '%로그인월_차단%')
# uc를 띄워도 절대 못 푸는 note — 죽은 페이지와 정책상 제외 도메인. '로그인월_차단'이 실제로는
# 404/502/삭제된 페이지까지 뭉뚱그려 붙어 있어서(2026-08-19 실측: 오류 페이지 198, 존재하지 않
# 40, 404 26, 인스타 30, 알리 4) 확대와 동시에 이 필터가 없으면 사람이 곁에 있는 패스가 죽은
# 링크 재방문으로 시간을 태운다. 근본적으로는 resolve 쪽에서 note를 분리해야 하지만(별건),
# 여기서는 큐에 안 넣는 것으로 막는다.
# ⚠ 마커는 전부 소문자로 — _has_dead_marker가 대소문자 무시로 비교한다. LLM이 쓰는 note가
# 같은 대상을 '인스타'와 'Instagram'으로 섞어 적어서(실측) 한쪽만 넣으면 절반이 샌다.
DEAD_NOTE_MARKERS = ('404', '존재하지 않', '오류 페이지', '페이지 없음', '사라진', '삭제된',
                     '서비스 종료', '인스타', 'instagram', '알리', 'aliexpress')
# 호스트가 RESOLVE_UC_HOSTS에 안 걸려도(예: candidate_url이 인포크 허브라 최종 목적지가 URL에
# 안 드러남) note가 안티봇/보안확인을 가리키면 uc가 뚫을 가치가 있다 — resolve_product가 허브를
# 다시 걸어가면서 최종 목적지에서 uc를 쓴다(호스트만으로 거르면 652건, 이걸 더하면 698건).
UC_ANTIBOT_MARKERS = ('보안', '캡차', '안티봇', '403', '429', '네이버')
# uc로도 계속 못 뚫는 건의 은퇴 상한. rescan_inprogress의 RESCAN_ERROR_MAX_ATTEMPTS(14)보다 훨씬
# 낮게 잡는다 — 그쪽은 무인 자동 패스라 재시도가 싸지만, 여기는 사람이 곁에 붙는 패스라 헛시도
# 하나의 비용이 비교가 안 된다. 3번 열어도 안 되면 캡차 정책이 바뀌기 전엔 안 될 가능성이 높다.
UC_MAX_ATTEMPTS = int(os.environ.get('UC_MAX_ATTEMPTS', '3'))
UC_STATE_FILE = ROOT / 'data/output/reverify_uc_state.jsonl'

# ── 시간 예산(2026-08-20) ────────────────────────────────────────────────────
# ⚠ 이 단계는 **큐를 처리하는 단계가 아니라 매일 조금씩 갉는 단계**다. 착각하면
# "왜 uc 큐가 안 줄지?"로 헤매게 되니 숫자를 여기 박아둔다(2026-08-20 실측):
#
#   유입: resolve 한 번이 만드는 uc 후보가 하루 약 436건
#         (로그인월_차단 69 + 재검증 중 차단 367 — 후자는 fast resolve가 uc 대상 호스트라
#          브라우저를 생략하고 넘긴 것이라 정의상 전부 uc 대상이다)
#   배수: uc는 uc_engine._lock으로 **전역 직렬**이라 REVERIFY_CONCURRENCY를 올려도 uc 페치
#         구간은 한 번에 하나다. UC_HARD_TIMEOUT=75초 기준 30분 예산의 처리량은
#            건당 15초 → 120건 / 건당 30초 → 60건 / 건당 75초(타임아웃) → 24건
#
# 즉 유입 436 vs 배수 24~120 — 매일 큐가 커진다(어제 확대 직후 702건이던 게 오늘 2,301건).
# 손익분기는 건당 30초 기준 하루 3.3시간이고, 그건 데일리 한 단계에 넣을 시간이 아니다.
# 그래서 이 값의 역할은 "큐를 없애는 것"이 아니라 **안전밸브**다 — uc가 죽거나 느려도 데일리가
# 묶이는 최대치를 이만큼으로 못박는다. 진짜 해법은 병렬화(detail의 UC_PROFILE 샤딩과 같은
# 패턴을 여기 적용하면 프로필 N개로 N배)이고, 그건 이 단계가 한 사이클 돌아 "건당 실제 몇 초"가
# 나온 뒤에 검토한다.
#
# 기본 0 = 예산 없음(예전 그대로 대상 전체를 끝까지) — 손으로 돌리는 실행의 동작을 안 바꾼다.
# daily가 이 단계를 돌릴 때만 1800(30분)을 넘긴다.
UC_TIME_BUDGET_SEC = float(os.environ.get('UC_TIME_BUDGET_SEC', '0'))
# 실행마다 (대상/시도/승격/잔량)을 한 줄 남긴다 — 다음 실행이 직전 잔량과 비교해 "유입"을
# 추정한다. 이 세 숫자(대상·배수·유입)가 같이 보여야 예산을 언제 늘릴지 판단이 된다.
UC_RUNS_FILE = ROOT / 'data/output/reverify_uc_runs.jsonl'
UPDATE_SQL = {code: product_update_link_sql(p) for code, p in PLATFORMS.items()}


def _has_dead_marker(note):
    """열어봐야 소용없는 죽은 페이지/제외 도메인 note인지(대소문자 무시)."""
    low = (note or '').lower()
    return any(m in low for m in DEAD_NOTE_MARKERS)


def _is_uc_addressable(note, candidate_url):
    """uc를 띄울 가치가 있는 건인지 — uc 대상 호스트이거나, note가 안티봇/보안확인을 가리킬 때.
    호스트 판정은 browser._uc_enabled_for에 위임한다(RESOLVE_UC_HOSTS를 호출 시점에 읽으므로
    이 모듈이 import 때 세팅한 값과 사용자가 덮어쓴 값이 그대로 반영된다)."""
    if candidate_url and _uc_enabled_for(candidate_url):
        return True
    return any(m in (note or '') for m in UC_ANTIBOT_MARKERS)


def classify_target(note, candidate_url, rec):
    """이 건을 이번 실행 대상에 넣을지. 반환 (대상 여부, 사유 라벨) — rescan_inprogress의
    classify_target과 같은 계약(순수 함수, 사유 라벨로 실행 시 분포를 찍는다)."""
    if rec and rec.get('attempts', 0) >= UC_MAX_ATTEMPTS:
        return False, f'은퇴(uc {UC_MAX_ATTEMPTS}회 실패)'
    if _has_dead_marker(note):
        return False, '제외(죽은 페이지/정책)'
    if not _is_uc_addressable(note, candidate_url):
        return False, '제외(uc 비대상)'
    return True, '대상'


def budget_exhausted(now, deadline):
    """시간 예산을 넘겼는지 — 예산이 꺼져 있으면(deadline=None) 항상 False. 순수 함수."""
    return deadline is not None and now >= deadline


def estimate_inflow(due_total, prev_run):
    """직전 실행 이후 큐에 새로 들어온 건수 추정 — (유입 or None, 근거 설명). 순수 함수.

    정확한 값이 아니라 추정이다: 이번 대상 수에서 직전 실행이 남긴 잔량을 뺀 것. 은퇴로 빠진
    건이 그 사이에 섞이면 과소추정되지만, "유입이 배수보다 큰가"라는 질문에는 이걸로 충분하다.
    """
    if not prev_run:
        return None, '직전 실행 기록 없음(이번이 첫 기록)'
    prev_remaining = prev_run.get('remaining_est')
    if prev_remaining is None:
        return None, '직전 기록에 잔량 없음'
    return due_total - prev_remaining, f"직전 실행 {prev_run.get('at', '?')} 잔량 {prev_remaining}건 대비"


def _last_run_record(path):
    """UC_RUNS_FILE의 마지막 줄 하나. load_jsonl은 'key' 필드를 요구하는 key-value 체크포인트용
    이라 여기(시계열 append 로그)에는 안 맞아서 따로 읽는다."""
    if not path.exists():
        return None
    last = None
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                last = line
    if not last:
        return None
    try:
        return json.loads(last)
    except ValueError:
        return None


def _select_sql(p):
    """차단으로 포기한 unresolved 상품. 컬럼 구성은 rescan_inprogress와 동일(parent 재구성 +
    resolve_product 입력). note는 파라미터로 넘겨 % 이스케이프 안전.

    SQL은 "차단 계열 note"라는 넓은 후보 풀만 가져오고, 죽은 페이지 제외·uc 대상 판정·은퇴는
    파이썬(classify_target)에서 한다 — rescan_inprogress와 같은 규약이다. SELECT는 싸고, 순수
    함수로 빼야 테스트가 되며, 실행할 때 사유별 분포를 찍어줄 수 있다."""
    parent_cols = ', '.join(f'p.{c}' for c in p.parent_ctx_cols)
    note_clause = ' OR '.join(['pp.link_note LIKE %s'] * len(BLOCKED_NOTE_LIKES))
    return f"""
SELECT pp.id AS row_id, pp.product_name, pp.link_location, pp.url_type, pp.candidate_url,
       pp.sort_order, pp.link_status, pp.link_note, {parent_cols}, p.classification_note
FROM {p.product_table} pp
JOIN {p.parent_table} p ON p.{p.id_col} = pp.{p.id_col}
WHERE pp.link_status = 'unresolved' AND ({note_clause})
"""


def _fetch_candidates(conn):
    out = []
    with conn.cursor() as cur:
        for code, p in PLATFORMS.items():
            cur.execute(_select_sql(p), BLOCKED_NOTE_LIKES)
            for r in cur.fetchall():
                parent = parent_ctx_from_row(p, r)
                product = {'product_name': r['product_name'], 'link_location': r['link_location'],
                           'url_type': r['url_type'], 'candidate_url': r['candidate_url'],
                           'sort_order': r['sort_order']}
                out.append((code, parent, product, r['row_id'], r['link_note']))
    return out


def main():
    acquire_lock('reverify_uc')
    if not DEEPSEEK_KEY:
        print('.env에 DEEPSEEK_KEY가 필요합니다.', file=sys.stderr)
        sys.exit(1)

    conn = connect_dst()
    try:
        candidates = _fetch_candidates(conn)
    finally:
        conn.close()

    state = load_jsonl(UC_STATE_FILE)
    targets, reasons = [], Counter()
    for code, parent, product, row_id, note in candidates:
        key = product_key(code, parent, product['sort_order'])
        due, reason = classify_target(note, product['candidate_url'], state.get(key))
        reasons[reason] += 1
        if due:
            targets.append((code, parent, product, row_id, key))

    due_total = len(targets)
    limit = int(os.environ.get('LIMIT', '0')) or due_total
    targets = targets[:limit]
    breakdown = ' / '.join(f'{k} {v}' for k, v in reasons.most_common())
    prev_run = _last_run_record(UC_RUNS_FILE)
    inflow, inflow_why = estimate_inflow(due_total, prev_run)
    print(f'차단 계열 unresolved 후보 풀 {len(candidates)}건 → 이번 실행 대상 {len(targets)}건'
          f"{f' (LIMIT으로 {due_total - len(targets)}건 보류)' if len(targets) < due_total else ''}")
    print(f'  분류: {breakdown}')
    print(f"  uc 엔진 ON(RESOLVE_UC={os.environ.get('RESOLVE_UC')}, "
          f"대상 호스트 {os.environ.get('RESOLVE_UC_HOSTS', 'naver.')}), 동시 {CONCURRENCY}, "
          f"브라우저 상한 {MAX_BROWSERS}, 은퇴 상한 {UC_MAX_ATTEMPTS}회")
    # ⚠ uc는 uc_engine._lock으로 전역 직렬 — 위 '동시 N'은 uc 페치 구간에는 안 먹는다(그 앞의
    # http/Playwright 시도와 LLM 호출에만 효과). 모듈 상단 UC_TIME_BUDGET_SEC 주석 참고.
    if UC_TIME_BUDGET_SEC > 0:
        print(f'  ⏱ 시간 예산 {UC_TIME_BUDGET_SEC / 60:.0f}분 — 이 단계는 큐를 비우는 단계가 아니라 '
              f'매일 조금씩 갉는 단계입니다(유입 > 배수).')
    if inflow is not None:
        print(f'  유입 추정 {inflow:+,}건 ({inflow_why})')
    if not targets:
        print('  재검증할 차단 건이 없습니다.')
        return

    counters = {}
    deadline = (time.monotonic() + UC_TIME_BUDGET_SEC) if UC_TIME_BUDGET_SEC > 0 else None

    def handle(ctx, target):
        code, parent, product, row_id, key = target
        db = ctx.state  # 워커당 DB 커넥션 1개
        # 예산을 넘겼으면 아무것도 안 하고 즉시 반환한다 — 큐가 빠르게 비워지면서 이 실행이
        # 정상 종료(exit 0)로 끝나고, 남은 건은 DB 상태 그대로라 다음 실행이 이어받는다.
        # 시도를 안 했으니 attempts도 안 올린다(은퇴가 억울하게 앞당겨지면 안 된다).
        if budget_exhausted(time.monotonic(), deadline):
            with ctx.lock:
                counters['_budget_skipped'] = counters.get('_budget_skipped', 0) + 1
            return
        try:
            res = resolve_product(ctx.page, code, parent, product)
        except Exception as e:
            res = {'status': 'error', 'final_url': None, 'note': f'예외: {str(e)[:150]}'}
        try:
            candidate_url = res.get('candidate_url') or product['candidate_url']
            new_candidate_url = candidate_url[:500] if candidate_url else None
            with ctx.lock:
                db.ping(reconnect=True)
                with db.cursor() as cur:
                    note = res.get('note')
                    cur.execute(UPDATE_SQL[code],
                                (new_candidate_url, res['status'], note[:255] if note else None, row_id))
                db.commit()
                # 시도 이력 — done이 되면 애초에 다음 실행의 후보 풀(unresolved)에서 빠지므로
                # attempts는 "uc로 열었는데도 못 풀린 횟수"가 되고, UC_MAX_ATTEMPTS에서 은퇴한다.
                prev = state.get(key) or {}
                attempts = prev.get('attempts', 0) + 1
                append_jsonl(UC_STATE_FILE, {'key': key, 'attempts': attempts,
                                             'last_status': res['status']})
                counters[res['status']] = counters.get(res['status'], 0) + 1
                counters['_n'] = counters.get('_n', 0) + 1
                # 이번 시도로 은퇴선에 도달한 건 — 다음 실행부터 큐에서 빠진다. 은퇴 정책
                # (UC_MAX_ATTEMPTS)이 도입 후 한 번도 안 돌아봐서(reverify_uc_state.jsonl 부재)
                # 실제로 걸리는지 눈으로 확인할 수 있게 따로 센다.
                if res['status'] != 'done' and attempts >= UC_MAX_ATTEMPTS:
                    counters['_retired'] = counters.get('_retired', 0) + 1
                shown = res.get('final_url') or res.get('note', '')
                print(f"  [{counters['_n']}/{len(targets)}] (w{ctx.worker_id}) row {row_id} -> "
                      f"{res['status']} {str(shown)[:70]}", flush=True)
        except Exception as e:
            with ctx.lock:
                counters['error'] = counters.get('error', 0) + 1
                counters['_n'] = counters.get('_n', 0) + 1
                print(f"  [{counters['_n']}/{len(targets)}] (w{ctx.worker_id}) row {row_id} -> "
                      f"저장 실패(스킵): {str(e)[:120]}", flush=True)

    run_crawl_pool(targets, handle, concurrency=CONCURRENCY, item_delay=ITEM_DELAY,
                   delay_only_after_browser=ITEM_DELAY_SMART,
                   worker_setup=connect_dst, worker_teardown=lambda db: db.close(),
                   warn_hint='REVERIFY_CONCURRENCY')

    by = {k: v for k, v in counters.items() if not k.startswith('_')}
    attempted = sum(by.values())
    promoted = counters.get('done', 0)
    skipped = counters.get('_budget_skipped', 0)
    retired = counters.get('_retired', 0)
    # 잔량 추정 — done으로 승격된 건과 이번에 은퇴선에 닿은 건이 다음 실행의 대상에서 빠진다.
    remaining_est = max(0, due_total - promoted - retired)

    print(f'2단 재검증 완료 — 대상 {due_total:,}건 중 {attempted:,}건 시도, done 승격 {promoted:,}건 — {by}')
    if skipped:
        print(f'  ⏱ 시간 예산({UC_TIME_BUDGET_SEC / 60:.0f}분) 소진으로 {skipped:,}건 미시도 — '
              f'다음 실행이 이어받습니다(시도 안 했으므로 은퇴 카운트에 안 들어갑니다).')
    if retired:
        print(f'  🚪 이번 시도로 은퇴({UC_MAX_ATTEMPTS}회 실패) 도달 {retired:,}건 — 다음 실행부터 큐에서 빠집니다.')
    print(f'  큐 잔량 추정 {remaining_est:,}건'
          + (f' (직전 실행 이후 유입 추정 {inflow:+,}건)' if inflow is not None else ''))

    append_jsonl(UC_RUNS_FILE, {
        'at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'due_total': due_total, 'attempted': attempted, 'promoted': promoted,
        'retired': retired, 'budget_skipped': skipped, 'remaining_est': remaining_est,
        'budget_sec': UC_TIME_BUDGET_SEC,
    })


if __name__ == '__main__':
    main()
