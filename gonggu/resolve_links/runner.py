"""체크포인트/CLI 진입점. 실제 판단 로직은 core.py에 있고, 워커 풀 배관은 crawl_pool.py
공용 모듈(2단계 B3)을 쓴다 — 여기는 "무엇을 해석 대상으로 삼고 결과를 어디에 남기는지"만 담당.

워커/브라우저 수명에 대한 실측 기반 설계(워커≠브라우저, MAX_BROWSERS 허가증,
release_if_contended)는 crawl_pool.py와 browser.py의 docstring 참고.

도메인당 동시 접근 상한(MAX_PER_DOMAIN)은 여기서 스케줄링 단위로 걸지 않는다 — 상품의
"첫 후보 URL" 도메인(예: 링크인바이오 허브)을 기준으로 걸면 실제 무거운 Playwright 접근이
일어나는 곳(LLM#2가 고른 최종 목적지, 전혀 다른 도메인일 수 있음)을 못 보호하면서 정작
가벼운 단계(캐시된 requests 호출)만 묶어두는 문제가 있었다(실측 확인, 2026-07-27). 대신
browser.fetch()/redirect.follow_redirect() 안에서 "실제로 page.goto()를 여는 그 순간" 목적지
도메인 기준으로 게이팅한다(domain_gate 참고)."""
import os
import re
import sys
import zlib

from gonggu import linkbio_parser
from gonggu.common import (DEEPSEEK_KEY, HIFEN_EMAIL_FILE, LOAD_READY_DIR, RESOLVED_DIR,
                     acquire_lock, append_jsonl, clear_json_dir, dump_jsonl_sharded,
                     load_json_dir, load_jsonl, parent_date_key)
from gonggu.crawl_pool import run_crawl_pool

from .config import (HTTP_FAST_PATH, HTTP_MIN_BODY_TEXT, ITEM_DELAY, ITEM_DELAY_SMART,
                     MAX_BROWSERS, RESOLUTION_FILE, RESOLVE_CONCURRENCY, RESOLVE_FAST_CONCURRENCY)
from .browser import permit_stats, set_allow_browser, via_stats
from .httpfetch import body_too_short_samples
from .httpfetch import stats as httpfetch_stats
from .core import resolve_product
from .links import extract_linkbio_hub_urls, load_persisted_linkbio_data
from .matching import product_key


def _dump_linkbio(items):
    """지금까지 파싱된 링크인바이오 허브 원본(인포크/링크트리/litt.ly 등 linkbio_parser가
    지원하는 플랫폼 전체, 2026-08-11부터 인포크 한정 해제)을 포스트별로 모아 게시일별 JSONL로
    저장한다.
    ⚠ 재크롤 없음 — 이미 파싱된 결과(load_persisted_linkbio_data, links.LINKBIO_HUB_CACHE_FILE
    영구 저장소)만 꺼내 쓰므로, crawl_linkbio의 독립 크롤을 데일리에서 대체한다. 허브가 실제로
    파싱된 포스트만 기록된다(증분). 이미 적재된 옛 포스트의 소급은 여전히 standalone
    `python3 -m gonggu.crawl_linkbio`가 담당.

    ⚠ 프로세스 로컬 캐시(links._linkbio_cache)가 아니라 파일 기반 영구 저장소를 쓴다
    (2026-08-18 점검, 문제 8 수정) — RESOLVE_SHARD_COUNT>1이면 이 함수는 실제 파싱이 일어난
    샤드 프로세스들과 전혀 다른 새 프로세스(`--finalize`)에서 호출되는데, 그 프로세스의
    메모리 캐시는 항상 비어있어서 예전 방식(cached_linkbio_data)으로는 조용히 0건이 됐다.

    곁다리로 그 허브 파싱본에서 연락 이메일도 같이 찾는다(linkbio_parser.extract_emails) —
    인스타그램 포스트(ig)면 계정(user_id)별로 HIFEN_EMAIL_FILE에도 남겨서
    `python3 -m gonggu.sync_hifen_emails`가 hifen DB에 반영할 수 있게 한다. dev_gongguking
    쪽 출력에는 영향 없음(이메일 컬럼 자체가 없음)."""
    from gonggu.crawl_linkbio import OUT_DIR  # 지연 import(순환참조 회피)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    persisted = load_persisted_linkbio_data()
    written = email_posts = 0
    for item in items:
        parent, code = item.get('parent') or {}, item.get('platform')
        nid = parent.get('post_id') if code == 'ig' else parent.get('video_id')
        if not nid:
            continue
        date = str(parent.get('publish_date') or parent.get('publishDate') or '')[:10]
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', date or ''):
            date = 'unknown'
        cand_text = ' '.join((p.get('candidate_url') or '') for p in item.get('products') or [])
        linkbio, post_emails = [], []
        for h in extract_linkbio_hub_urls(cand_text):
            data = persisted.get(h)
            if not data:
                continue
            found = linkbio_parser.extract_emails(data)
            linkbio.append({'hub_url': h, 'parsed': data, 'error': None, 'emails': found or None})
            for e in found:
                if e not in post_emails:
                    post_emails.append(e)
        if linkbio:
            rec = {'key': f'{code}:{nid}', 'platform': code, 'post_id': nid,
                   'publish_date': date, 'hub_urls': [x['hub_url'] for x in linkbio],
                   'linkbio': linkbio}
            if post_emails:
                rec['emails'] = ','.join(post_emails)
            append_jsonl(OUT_DIR / f'{date}.jsonl', rec)
            written += 1
            user_id = parent.get('user_id') if code == 'ig' else None
            if post_emails and user_id:
                append_jsonl(HIFEN_EMAIL_FILE,
                             {'key': user_id, 'user_id': user_id, 'emails': post_emails,
                              'source_post': f'{code}:{nid}'})
                email_posts += 1
    if written:
        print(f'  링크인바이오 파싱본 저장: {written}개 포스트 -> data/linkbio/<게시일>.jsonl (재크롤 없음)')
    if email_posts:
        print(f'  이메일 발견: 인스타그램 계정 {email_posts}개 -> {HIFEN_EMAIL_FILE} '
              f'(hifen DB 반영은 별도로 `python3 -m gonggu.sync_hifen_emails`)')


# 진단(미스 사유/via 분포)을 끝에 한 번만 찍으면, 오늘처럼 남은 물량이 커서(5,084건) 실행이
# 오래 걸리는 날은 다 끝날 때까지 아무 근거 데이터도 못 본다(2026-08-18, 사용자 요청으로 추가).
# 이번 실행에서 처리한 건수 기준으로 N건마다 중간 스냅샷도 찍는다 — 체크포인트 재개 시 이미
# 끝난 옛 건수(done_n)가 아니라 "이번 프로세스가 실제로 처리한 건수"를 기준으로 삼아야
# 재개 직후 바로 한 번 더 찍히는 식의 우연한 어긋남이 없다. 0이면 끔.
DIAG_INTERVAL = int(os.environ.get('RESOLVE_DIAG_INTERVAL', '500'))

# ── 워커 풀 프로세스 샤딩(2026-08-18, 속도개선 공사 다음 라운드 — E) ──
# 한 프로세스 안의 크롤 풀이 스톨(드라이버 먹통)에 걸리면 그 프로세스 전체(워커 60개분)가
# 최대 STAGE_STALL_RETRIES회까지 통째로 멈췄다 재시작한다 — 이상치 포스트(상품 수십 개가
# 같은 후보 URL 공유) 하나가 전체 처리량을 잠깐이지만 반복적으로 볼모로 잡는 실측 확인
# (2026-08-18, gonggumoa/V6RsKzkf7NA 사례). RESOLVE_SHARD_COUNT>1이면 pending을 key 기준
# 결정론적 해시로 N등분해, 각 샤드를 별도 OS 프로세스(daily.py가 띄움)로 돌린다 — 한 샤드가
# 스톨나도 나머지 샤드는 계속 진행하므로 "전체 정지" 대신 "일부만 느려짐"이 된다.
# ⚠ zlib.crc32를 쓴다(내장 hash()가 아님) — 문자열 hash()는 PYTHONHASHSEED가 프로세스마다
# 랜덤이라 같은 key라도 프로세스(샤드)마다 다른 값이 나올 수 있어 파티션이 어긋난다.
RESOLVE_SHARD_COUNT = int(os.environ.get('RESOLVE_SHARD_COUNT', '1'))
RESOLVE_SHARD_INDEX = int(os.environ.get('RESOLVE_SHARD_INDEX', '0'))


def _shard_index(key, shard_count):
    """key를 [0, shard_count) 중 하나로 결정론적으로 배정 — 같은 key는 항상 같은 샤드로
    간다(프로세스가 몇 번을 다시 떠도, 다른 프로세스에서 계산해도 동일)."""
    return zlib.crc32(key.encode('utf-8')) % shard_count


def _should_print_interim_diag(processed_this_run, interval):
    return interval > 0 and processed_this_run % interval == 0


def _bucket_body_lengths(samples, threshold):
    """body_too_short 표본을 문턱(threshold) 대비 세 구간으로 나눈다 — '문턱을 낮추면 구제될
    근접 사례'인지 '본문이 거의 없어 문턱을 만져도 소용없는 사례'인지 구분하는 게 목적이라,
    경계는 threshold의 20%/80% 지점으로 잡는다(정교한 통계가 아니라 방향성 판단용)."""
    near_zero = mid = near_threshold = 0
    for _host, length in samples:
        if length < threshold * 0.2:
            near_zero += 1
        elif length >= threshold * 0.8:
            near_threshold += 1
        else:
            mid += 1
    return {'near_zero': near_zero, 'mid': mid, 'near_threshold': near_threshold}


def _print_resolution_diagnostics(hs, via, body_samples=()):
    """속도 개선 공사 A단계(2026-08-18) — 지금까지는 '패스트패스 적중률' 합계 하나만 찍어서,
    적중률이 날마다 12~60%로 들쭉날쭉해도(2026-08-07~13 실측) 왜 낮은지·실제로 브라우저까지
    간 비율이 얼마인지는 알 수 없었다. B(fast-path 필터 보정)와 C(워커:브라우저 비율 재튜닝)는
    감이 아니라 이 실측을 근거로 결정한다. body_samples는 B단계(2026-08-18) 추가 — 실측 결과
    미스의 75~80%가 body_too_short 하나였어서, 그게 '문턱 근접'인지 '본문 자체가 없음(JS
    렌더링)'인지까지 파고든다."""
    if hs['tried']:
        print(f"requests 패스트패스: {hs['hit']}/{hs['tried']}건 적중 "
              f"({100 * hs['hit'] / hs['tried']:.1f}%) — 나머지는 브라우저로 폴백")
        misses = sorted(((k[len('miss:'):], v) for k, v in hs.items() if k.startswith('miss:')),
                        key=lambda kv: -kv[1])
        if misses:
            top = ', '.join(f'{reason} {n}건' for reason, n in misses[:8])
            print(f'  ㄴ 패스트패스 미스 사유(많은 순): {top}')
    if body_samples:
        b = _bucket_body_lengths(body_samples, HTTP_MIN_BODY_TEXT)
        lo, hi = int(HTTP_MIN_BODY_TEXT * 0.2), int(HTTP_MIN_BODY_TEXT * 0.8)
        print(f'  ㄴ body_too_short 길이 분포(문턱 {HTTP_MIN_BODY_TEXT}자, 표본 {len(body_samples)}건): '
              f'0~{lo}자 {b["near_zero"]}건(본문 자체가 거의 없음 — 문턱 조정 무의미, JS 렌더링 추정), '
              f'{lo}~{hi}자 {b["mid"]}건, '
              f'{hi}~{HTTP_MIN_BODY_TEXT}자 {b["near_threshold"]}건(문턱 살짝 낮추면 통과 가능)')
    total_via = sum(via.values())
    if total_via:
        # linkbio_structured/uc_host_skip은 fetch() 자체를 안 부른 경우, http/browser/uc는
        # fetch()가 실제로 고른 경로 — 이 다섯 합이 "이번 실행에서 시도한 후보 URL 페치 전체"다.
        order = ['linkbio_structured', 'uc_host_skip', 'http', 'browser', 'uc']
        parts = [f'{k} {via[k]}건({100 * via[k] / total_via:.0f}%)' for k in order if via.get(k)]
        parts += [f'{k} {v}건' for k, v in via.items() if k not in order]
        print(f'  ㄴ 후보 URL 처리 경로(후보 URL 페치 시도 {total_via}건 기준, 상품 건수와 다름): '
              + ', '.join(parts))
    _print_permit_diagnostics(permit_stats())


def _print_permit_diagnostics(ps):
    """브라우저 허가증(MAX_BROWSERS개)을 쥔 채 실제로는 브라우저를 안 쓴 시간의 비율.

    한 상품은 브라우저→LLM#3→브라우저→LLM#2→…를 오가는데, 허가증은 그 전체 구간 동안 잡혀
    있다(LazyPage는 상품 경계에서만 놓는다). LLM을 기다리는 동안에도 14개뿐인 허가증 하나가
    묶여 있다는 뜻이다. 이 비율이 높으면 "크롤 단계와 LLM 단계를 분리"하는 구조 변경이 값을
    하고, 낮으면 지금 구조가 이미 알차게 쓰고 있는 것이다 — 감이 아니라 이 숫자로 정한다.
    (허가증을 LLM 직전에 놓는 단순 처방은 답이 아니다: 허가증=살아있는 크롬 수라 놓으려면
    닫아야 하고 재기동에 3.9초가 든다. 무조건 넘기기가 1.8배 느렸다는 실측도 이미 있다 —
    browser.LazyPage.release_if_contended 주석.)"""
    if not ps.get('sessions') or ps.get('idle_ratio') is None:
        return
    held, busy = ps['held_sec'], ps['busy_sec']
    print(f"  ㄴ 브라우저 허가증 점유: 총 {held:.0f}초 중 실제 브라우저 작업 {busy:.0f}초 "
          f"— 유휴 {ps['idle_ratio']:.0%} (허가증 세션 {ps['sessions']}회)")
    # 유휴율의 나머지 반쪽 — 실제로 줄 선 시간이 있어야 그 유휴가 손해다.
    wait, waits = ps.get('wait_sec', 0.0), ps.get('waits', 0)
    if waits:
        print(f"     ㄴ 허가증 대기: 총 {wait:.0f}초 / {waits}회 (평균 {wait / waits:.1f}초) "
              f"— 크롤/LLM 단계를 분리하면 이 대기 중 상당 부분을 회수할 수 있다")
    else:
        print('     ㄴ 허가증 대기: 없음 — 아무도 안 기다렸으니 위 유휴는 손해가 아니다'
              '(구조 변경으로 회수할 게 없음)')


def load_resolutions():
    """key당 res를 담은 dict로 복원 — resolutions[key]에는 'key' 필드가 없어야 기존
    build_resolved_file 등 소비 코드가 그대로 동작하므로 로드 시 벗겨낸다."""
    raw = load_jsonl(RESOLUTION_FILE)
    return {k: {kk: vv for kk, vv in rec.items() if kk != 'key'} for k, rec in raw.items()}


def build_resolved_file(items, resolutions):
    out = []
    for item in items:
        platform, parent = item['platform'], item['parent']
        new_products = []
        for p in item['products']:
            key = product_key(platform, parent, p['sort_order'])
            res = resolutions.get(key)
            np = dict(p)
            # link_status = 이 candidate_url이 검증된 최종 상품페이지(done)인지, 아니면 아직
            # 확인 못 한 중간 단계(unresolved/hold/error)인지 — 개발자가 "바로 스크래핑 가능한지
            # vs 더 파고들어야 하는지" 판단할 수 있게 남겨둔다. url_type은 원본 후보의 종류를
            # 그대로 유지해서(덮어쓰지 않음) 디버깅용 정보를 보존한다.
            # candidate_url은 상태와 무관하게 항상 단일 URL이어야 한다(2026-07-29 결정, DB의
            # candidate_url엔 세미콜론 구분 원본 후보 목록을 절대 남기지 않음) — resolve_product가
            # 이미 성공/실패 어느 쪽이든 대표 URL 1개를 candidate_url 필드에 담아 반환한다.
            np['link_status'] = res.get('status') if res else None
            if res and res.get('candidate_url'):
                np['candidate_url'] = res['candidate_url'][:500]
            # link_note = 이 상태가 왜 나왔는지(LLM#3의 reason 또는 "후보 링크 없음/로그인월
            # 차단/상품명 너무 일반적" 같은 결정적 사유). 이미 core.py가 상품별로 만들어 두는
            # 것을 DB 상품 행까지 실어 나른다(파일에만 있던 걸 건바이건으로 보존). VARCHAR(255)
            # 안전하게 자른다(core.py에서 이미 짧게 잘리지만 방어적).
            note = res.get('note') if res else None
            np['link_note'] = note[:255] if note else None
            new_products.append(np)
        out.append({**item, 'products': new_products})
    # items+resolutions로 매번 전체를 다시 조립하므로, 재계산 후 특정 날짜에 남는 레코드가
    # 없어졌는데 옛 날짜 파일이 안 지워져 stale로 남는 걸 막기 위해 먼저 비운다.
    clear_json_dir(RESOLVED_DIR)
    dump_jsonl_sharded(RESOLVED_DIR, out, parent_date_key)


def finalize(items, resolutions):
    """resolve 루프가 끝난 뒤 결과를 소비 가능한 형태로 남기는 부분 — 샤딩된 실행에서는 각
    샤드가 이걸 부르면 안 된다(자기 샤드만 아는 불완전한 resolutions로 RESOLVED_DIR를 통째로
    지우고 다시 쓰게 되어, 동시에 돌던 다른 샤드의 결과가 최종 산출물에서 누락된다 — 2026-08-18
    설계 검토). 그래서 샤딩 시에는 전 샤드가 끝난 뒤 `--finalize`로 한 번만 별도 호출한다
    (그 시점엔 RESOLUTION_FILE에 모든 샤드의 결과가 이미 append돼 있으므로 load_resolutions()가
    전체를 정확히 복원한다)."""
    build_resolved_file(items, resolutions)
    _dump_linkbio(items)   # 인포크 파싱본을 게시일별 JSON으로(재크롤 없이 캐시에서)
    by_status = {}
    for r in resolutions.values():
        by_status[r['status']] = by_status.get(r['status'], 0) + 1
    # 패스트패스 적중률+미스 사유+실제 경로 분포를 남긴다 — "브라우저는 거의 안 뜬다"는 전제가
    # 코드 주석에만 남고 실제로는 무너져 있었던 게 성능 저하의 원인이었다(2026-08-01). 매 실행마다
    # 실측치를 찍어둔다(2026-08-18, 속도 개선 공사 A단계로 미스 사유·via 분포까지 확장).
    _print_resolution_diagnostics(httpfetch_stats(), via_stats(), body_too_short_samples())
    print(f'누적 {len(resolutions)}건 — {by_status} -> {RESOLVED_DIR}/*.jsonl (날짜별)')


def _resolve_pending(pending, resolutions, total, shard_tag=''):
    """Tier0(브라우저 없는 빠른 패스) -> Tier1(브라우저 필요분만) 순으로 pending을 처리해
    resolutions에 결과를 채운다(부수효과, JSONL에도 append). main()과 테스트 양쪽에서 이 함수
    하나만 검증하면 된다(daily.py의 _run_resolve_links_sharded와 같은 추출 패턴)."""
    processed_this_run = 0
    needs_browser_rows = []

    def _finish(key, res, ctx, phase_tag):
        nonlocal processed_this_run
        shown = res.get('final_url') or res.get('note', '')
        with ctx.lock:
            resolutions[key] = res
            done_n = len(resolutions)
            processed_this_run += 1
            print(f'  [{done_n}/{total}]{shard_tag}{phase_tag} (w{ctx.worker_id}) {key} -> '
                  f'{res["status"]} {shown[:70]}', flush=True)
            # 결과 1건 = 파일 끝에 한 줄 추가(append)만 — 건수가 쌓여도 이 저장이 lock을
            # 오래 붙잡지 않는다(2026-07-27 실측/전환, common.append_jsonl 참고). 여러 샤드
            # 프로세스가 동시에 같은 RESOLUTION_FILE에 append해도 각자 다른 key만 쓰므로
            # (파티션이 겹치지 않음) 파일 레벨 충돌은 없다 — append_jsonl은 매 호출마다 열고
            # 닫는 단순 append라 OS가 보장하는 개별 write 원자성에 의존한다.
            append_jsonl(RESOLUTION_FILE, {**res, 'key': key})
            if _should_print_interim_diag(processed_this_run, DIAG_INTERVAL):
                print(f'  ── 중간 진단{shard_tag}(이번 실행 {processed_this_run}건 처리 시점) ──', flush=True)
                _print_resolution_diagnostics(httpfetch_stats(), via_stats(), body_too_short_samples())

    # ── Tier0: 브라우저 없는 빠른 패스(2026-08-18, 속도개선 공사 F단계) ──
    # 실측(via_stats, 2026-08-18) 후보 URL의 약 80%(linkbio_structured+uc_host_skip+http 합)가
    # 브라우저를 아예 안 쓰고 끝나는데, 예전엔 이 80%도 브라우저 필요분까지 감안해 낮게 잡은
    # RESOLVE_CONCURRENCY 슬롯 수만큼만 병렬화됐다. set_allow_browser(False)로 브라우저 분기를
    # 전부 막고(core.py/picker.py가 즉시 status='needs_browser'로 넘긴다), use_playwright=False로
    # Playwright 드라이버 자체도 안 띄운 채(워커 수만큼 Node 프로세스를 띄우는 비용까지 제거)
    # RESOLVE_FAST_CONCURRENCY(기본 200)로 전 pending을 한 번 훑는다. 여기서 끝나지 않은
    # (needs_browser) 건만 모아 Tier1로 넘긴다.
    def handle_fast(ctx, row):
        key, item, p = row
        try:
            res = resolve_product(ctx.page, item['platform'], item['parent'], p)
        except Exception as e:
            res = {'status': 'error', 'final_url': None, 'note': str(e)[:160]}
        if res['status'] == 'needs_browser':
            with ctx.lock:
                needs_browser_rows.append(row)
            return
        _finish(key, res, ctx, '(빠른)')

    set_allow_browser(False)
    try:
        run_crawl_pool(pending, handle_fast, concurrency=RESOLVE_FAST_CONCURRENCY,
                       item_delay=0, use_playwright=False)
    finally:
        set_allow_browser(True)

    # ── Tier1: 브라우저 필요분만(기존 경로) ──
    if needs_browser_rows:
        print(f'[Tier1: 브라우저 필요]{shard_tag} {len(needs_browser_rows)}건 — 동시 워커 '
              f'{RESOLVE_CONCURRENCY}개, 브라우저 상한 {MAX_BROWSERS}개', flush=True)

        def handle_browser(ctx, row):
            key, item, p = row
            try:
                res = resolve_product(ctx.page, item['platform'], item['parent'], p)
            except Exception as e:
                res = {'status': 'error', 'final_url': None, 'note': str(e)[:160]}
            if res['status'] == 'needs_browser':
                # 브라우저를 허용한 패스에서 또 나오면 내부 로직 오류다(무한 보류 방지로
                # error 강등 — 재실행하면 다시 pending에 잡혀 재시도된다).
                res = {'status': 'error', 'final_url': None,
                       'note': '브라우저 패스(Tier1)에서도 needs_browser — 내부 로직 오류로 추정'}
            _finish(key, res, ctx, '(브라우저)')

        run_crawl_pool(needs_browser_rows, handle_browser, concurrency=RESOLVE_CONCURRENCY,
                       item_delay=ITEM_DELAY, delay_only_after_browser=ITEM_DELAY_SMART,
                       warn_hint='RESOLVE_CONCURRENCY')
    else:
        print(f'[Tier1 생략]{shard_tag} 전부 Tier0(빠른 패스)에서 처리됨', flush=True)


def main():
    if '--finalize' in sys.argv:
        # 샤딩된 실행들이 전부 끝난 뒤 daily.py가 한 번만 호출하는 소비 전용 모드 — 크롤/락 없이
        # RESOLUTION_FILE(모든 샤드가 이미 append 완료)만 읽어 RESOLVED_DIR을 재조립한다.
        items = load_json_dir(LOAD_READY_DIR)
        finalize(items, load_resolutions())
        return

    shard_count = max(1, RESOLVE_SHARD_COUNT)
    shard_index = RESOLVE_SHARD_INDEX
    lock_name = f'resolve_links_shard{shard_index}' if shard_count > 1 else 'resolve_links'
    acquire_lock(lock_name)
    if not DEEPSEEK_KEY:
        print('.env에 DEEPSEEK_KEY가 필요합니다.', file=sys.stderr)
        sys.exit(1)

    items = load_json_dir(LOAD_READY_DIR)
    resolutions = load_resolutions()

    pending = [
        (product_key(item['platform'], item['parent'], p['sort_order']), item, p)
        for item in items for p in item['products']
    ]
    pending = [(k, item, p) for k, item, p in pending if k not in resolutions]
    if shard_count > 1:
        pending = [(k, item, p) for k, item, p in pending if _shard_index(k, shard_count) == shard_index]
    if len(sys.argv) > 1 and sys.argv[1].lstrip('-').isdigit():
        pending = pending[:int(sys.argv[1])]

    shard_tag = f' [샤드 {shard_index}/{shard_count}]' if shard_count > 1 else ''
    print(f'해석 대상{shard_tag} {len(pending)}건 (이미 처리됨 {len(resolutions)}건) — '
          f'Tier0(브라우저 없는 빠른 패스) 동시성 {RESOLVE_FAST_CONCURRENCY}개, '
          f'Tier1(브라우저 패스) 동시 워커 {RESOLVE_CONCURRENCY}개/브라우저 상한 {MAX_BROWSERS}개, '
          f'requests 패스트패스 {"ON" if HTTP_FAST_PATH else "OFF"}')

    if pending:
        total = len(resolutions) + len(pending)
        _resolve_pending(pending, resolutions, total, shard_tag)

    if shard_count > 1:
        # 이 샤드 몫만 끝났다 — RESOLVED_DIR 재조립(finalize)은 전 샤드가 끝난 뒤 daily.py가
        # `--finalize`로 한 번만 부른다(위 finalize() docstring 참고).
        print(f'샤드 {shard_index}/{shard_count} 완료 — 이 샤드가 처리한 {len(pending)}건 반영됨 '
              f'(RESOLVED_DIR 재조립은 전 샤드 완료 후 --finalize가 담당)')
        return

    finalize(items, resolutions)


if __name__ == '__main__':
    main()
