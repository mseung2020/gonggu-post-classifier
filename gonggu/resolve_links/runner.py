"""워커 풀 실행/체크포인트/CLI 진입점. 실제 판단 로직은 core.py에 있고, 여기는 그걸
여러 상품에 대해 동시에·안전하게 돌리는 배관(plumbing)만 담당한다."""
import queue
import sys
import threading
import time

from playwright.sync_api import sync_playwright

from gonggu.common import (DEEPSEEK_KEY, LOAD_READY_DIR, RESOLVED_DIR, append_jsonl, clear_json_dir,
                     dump_jsonl_sharded, load_json_dir, load_jsonl, parent_date_key)

from .browser import LazyPage
from .config import HTTP_FAST_PATH, ITEM_DELAY, MAX_BROWSERS, RESOLUTION_FILE, RESOLVE_CONCURRENCY
from .httpfetch import stats as httpfetch_stats
from .core import resolve_product
from .matching import product_key


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
            new_products.append(np)
        out.append({**item, 'products': new_products})
    # items+resolutions로 매번 전체를 다시 조립하므로, 재계산 후 특정 날짜에 남는 레코드가
    # 없어졌는데 옛 날짜 파일이 안 지워져 stale로 남는 걸 막기 위해 먼저 비운다.
    clear_json_dir(RESOLVED_DIR)
    dump_jsonl_sharded(RESOLVED_DIR, out, parent_date_key)


def _resolve_worker(worker_id, work_q, resolutions, lock, total, save_auth_state):
    """워커 1개 = 독립된 Playwright 인스턴스 1개. work_q에서 하나씩 꺼내 처리 — Playwright
    sync API는 스레드마다 별도 인스턴스를 쓰는 게 권장 방식이라 스레드끼리 공유하지 않는다.
    브라우저 자체는 LazyPage가 실제로 필요해지는 첫 순간까지 미루고, 동시에 뜨는 개수도
    MAX_BROWSERS로 제한한다(browser.py 참고) — 워커 수만큼 무조건 브라우저가 뜨던 예전
    구조는 워커 200개에서 크롬 관련 프로세스 550개+·스왑 32GB 소진으로 시스템이 먹통이 되는
    걸로 실측 확인됐다(2026-07-30).

    상품 하나를 끝낼 때마다 release_if_contended()를 부른다 — 예전엔 브라우저를 한 번 잡으면
    큐가 다 빌 때까지 안 놓아서, 허가증을 못 받은 워커는 그동안 값싼 건(패스트패스로 끝나는
    건)조차 처리하지 못하고 멈춰 있었다. 브라우저 작업 자체의 처리량은 어차피 MAX_BROWSERS가
    상한이라 극적으로 빨라지진 않지만, 놀고 있던 워커가 값싼 건을 소화하게 된다(실측,
    2026-08-01 — 브라우저 필요 비율 30% 시뮬레이션에서 1.3배, 60% 이상에선 차이 없음).

    같은 사이트에 동시 요청이 몰리는 걸 막는 도메인당 상한(MAX_PER_DOMAIN)은 여기서 스케줄링
    단위로 걸지 않는다 — 상품의 "첫 후보 URL" 도메인(예: 링크인바이오 허브)을 기준으로 걸면
    실제 무거운 Playwright 접근이 일어나는 곳(LLM#2가 고른 최종 목적지, 전혀 다른 도메인일
    수 있음)을 못 보호하면서 정작 가벼운 단계(캐시된 requests 호출)만 묶어두는 문제가
    있었다(실측 확인, 2026-07-27). 대신 browser.fetch()/redirect.follow_redirect() 안에서
    "실제로 page.goto()를 여는 그 순간" 목적지 도메인 기준으로 게이팅한다(domain_gate 참고)."""
    with sync_playwright() as pw:
        page = LazyPage(pw, save_auth_state=save_auth_state)
        while True:
            try:
                key, item, p = work_q.get_nowait()
            except queue.Empty:
                break
            try:
                res = resolve_product(page, item['platform'], item['parent'], p)
            except Exception as e:
                res = {'status': 'error', 'final_url': None, 'note': str(e)[:160]}
            shown = res.get('final_url') or res.get('note', '')
            with lock:
                resolutions[key] = res
                done_n = len(resolutions)
                print(f'  [{done_n}/{total}] (w{worker_id}) {key} -> {res["status"]} {shown[:70]}', flush=True)
                # 결과 1건 = 파일 끝에 한 줄 추가(append)만 한다 — 예전엔 10건마다 dict
                # 전체를 다시 직렬화해서 저장했는데, resolutions가 커질수록(1만 건에서
                # 11.7초) 그 저장이 전체 워커가 공유하는 이 lock 안에서 일어나 다른 워커들도
                # 같이 멈춰 섰다(실측 확인, 2026-07-27 — "처음엔 잘 되다가 건수가 쌓이면서
                # 점점 느려짐"의 원인). append는 건수와 무관하게 항상 거의 즉시 끝나므로
                # lock을 오래 붙잡을 일이 없다.
                append_jsonl(RESOLUTION_FILE, {**res, 'key': key})
            # 대기 중인 워커한테 브라우저 허가증을 넘겨줄지 판단하는 건 sleep 전에 — 어차피
            # 3초 자는 동안 브라우저를 붙잡고 있을 이유가 없다.
            page.release_if_contended()
            time.sleep(ITEM_DELAY)
        page.close()


def main():
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
    if len(sys.argv) > 1:
        pending = pending[:int(sys.argv[1])]

    n_workers = max(1, min(RESOLVE_CONCURRENCY, len(pending) or 1))
    print(f'해석 대상 {len(pending)}건 (이미 처리됨 {len(resolutions)}건) — 동시 워커 {n_workers}개, '
          f'브라우저 상한 {MAX_BROWSERS}개, requests 패스트패스 {"ON" if HTTP_FAST_PATH else "OFF"}')
    # 워커 수가 브라우저 상한보다 훨씬 많으면 브라우저가 필요한 워커들이 허가증을 주고받느라
    # 재기동(3.9초)을 반복한다 — 굶지는 않지만(release_if_contended) 처리량이 떨어지므로,
    # 조용히 느려지는 대신 눈에 보이게 알린다.
    if n_workers > MAX_BROWSERS * 3:
        print(f'  ⚠ 워커({n_workers})가 브라우저 상한({MAX_BROWSERS})의 3배를 넘습니다 — 브라우저가 '
              f'필요한 건이 많으면 재기동 오버헤드로 오히려 느려질 수 있습니다. '
              f'MAX_BROWSERS를 올리거나 RESOLVE_CONCURRENCY를 낮춰보세요.')

    if pending:
        total = len(resolutions) + len(pending)
        work_q = queue.Queue()
        for row in pending:
            work_q.put(row)
        lock = threading.Lock()
        threads = [
            threading.Thread(target=_resolve_worker, args=(wid, work_q, resolutions, lock, total, wid == 0))
            for wid in range(n_workers)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    build_resolved_file(items, resolutions)
    by_status = {}
    for r in resolutions.values():
        by_status[r['status']] = by_status.get(r['status'], 0) + 1
    # 패스트패스 적중률을 남긴다 — "브라우저는 거의 안 뜬다"는 전제가 코드 주석에만 남고 실제로는
    # 무너져 있었던 게 이번 성능 저하의 원인이었다(2026-08-01). 매 실행마다 실측치를 찍어둔다.
    hs = httpfetch_stats()
    if hs['tried']:
        print(f"requests 패스트패스: {hs['hit']}/{hs['tried']}건 적중 "
              f"({100 * hs['hit'] / hs['tried']:.1f}%) — 나머지는 브라우저로 폴백")
    print(f'누적 {len(resolutions)}건 — {by_status} -> {RESOLVED_DIR}/*.jsonl (날짜별)')


if __name__ == '__main__':
    main()
