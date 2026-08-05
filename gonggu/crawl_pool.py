"""크롤링 워커 풀 공통 배관(대공사 2단계 B3, 2026-08-05).

runner.py / rescan_inprogress.py / backfill_period.py / _diag_sample.py 네 곳에 큐 + 스레드 +
Playwright/LazyPage 수명 관리가 각각 손으로 복제되어 있었고, 그 "미묘한 차이"가 실제 사고로
이어졌다(감사 A1 — rescan에는 적용된 LazyPage 안전판이 backfill에는 누락). 이제 배관은 여기
한 곳에만 있고, 각 스크립트는 "항목 하나를 어떻게 처리하는가"(handle)만 정의한다.

보존되는 동작(전부 기존 스크립트들의 실측 기반 설계 그대로):
- 워커 1개 = 독립 Playwright 인스턴스 1개(sync API는 스레드 간 공유 금지).
- 브라우저는 LazyPage — 실제 필요해지는 첫 순간까지 생성을 미루고, 동시 개수는
  MAX_BROWSERS 허가증으로 제한(2026-07-30 스왑 32GB 사고 재발 방지).
- 항목 하나 끝날 때마다 release_if_contended() — 기다리는 워커가 있으면 브라우저를 넘겨줌.
- 세션 저장(save_auth_state)은 워커 0번만.
- handle에서 예외가 새어나와도 그 항목만 실패 로그를 남기고 워커는 계속 돈다 — 예전 rescan에서
  예외로 스레드가 조용히 죽어 실제 동시성이 줄어들던 사고의 재발 방지(2026-08-04 실측).
"""
import queue
import threading
import time
from types import SimpleNamespace

from playwright.sync_api import sync_playwright

from gonggu.resolve_links.browser import LazyPage
from gonggu.resolve_links.config import MAX_BROWSERS


def run_crawl_pool(items, handle, *, concurrency, item_delay=0.0,
                   worker_setup=None, worker_teardown=None,
                   save_auth_first_worker=True, warn_hint=None):
    """items를 워커 스레드들이 나눠 handle로 처리한다.

    - handle(ctx, item): 항목 하나의 처리 전부(크롤링/판단/저장/로그). ctx 속성:
        ctx.page      : LazyPage (그냥 Page처럼 쓰면 됨 — 첫 사용 때 브라우저가 뜬다)
        ctx.worker_id : 워커 번호
        ctx.lock      : 전 워커 공유 lock — 저장/카운터/print는 이걸로 감쌀 것
        ctx.state     : worker_setup()의 반환값(예: 워커당 DB 커넥션)
    - worker_setup() -> state / worker_teardown(state): 워커당 자원 생성/정리.
    - item_delay: 항목 사이 대기(초, 워커별) — 안티봇/레이트리밋 완화(resolve/rescan은
      ITEM_DELAY, backfill은 0).
    - warn_hint: 워커 수가 MAX_BROWSERS의 3배를 넘을 때 경고에 넣을 환경변수 이름
      (예: 'RESOLVE_CONCURRENCY') — 없으면 경고 생략.

    반환: 실제 워커 수."""
    n_workers = max(1, min(concurrency, len(items) or 1))
    if warn_hint and n_workers > MAX_BROWSERS * 3:
        print(f'  ⚠ 워커({n_workers})가 브라우저 상한({MAX_BROWSERS})의 3배를 넘습니다 — 브라우저가 '
              f'필요한 건이 많으면 재기동 오버헤드로 오히려 느려질 수 있습니다. '
              f'MAX_BROWSERS를 올리거나 {warn_hint}를 낮춰보세요.')

    work_q = queue.Queue()
    for item in items:
        work_q.put(item)
    lock = threading.Lock()

    def _worker(wid):
        state = worker_setup() if worker_setup else None
        try:
            with sync_playwright() as pw:
                page = LazyPage(pw, save_auth_state=(save_auth_first_worker and wid == 0))
                ctx = SimpleNamespace(page=page, worker_id=wid, lock=lock, state=state)
                while True:
                    try:
                        item = work_q.get_nowait()
                    except queue.Empty:
                        break
                    try:
                        handle(ctx, item)
                    except Exception as e:
                        # 여기까지 새어나온 예외 = handle이 자체 처리 못 한 예상 밖 상황.
                        # 이 항목만 포기하고 워커는 계속 돈다(스레드가 죽으면 남은 물량을
                        # 다시는 안 가져가서 동시성이 조용히 줄어든다 — 2026-08-04 실측).
                        with lock:
                            print(f'  ⚠ (w{wid}) 항목 처리 중 예외 — 이 항목만 건너뜀: {str(e)[:120]}',
                                  flush=True)
                    # 브라우저를 더 안 쓰게 됐는데 기다리는 워커가 있으면 넘겨준다 — sleep 전에
                    # (어차피 자는 동안 브라우저를 붙잡고 있을 이유가 없다).
                    page.release_if_contended()
                    if item_delay:
                        time.sleep(item_delay)
                page.close()
        finally:
            if worker_teardown and state is not None:
                worker_teardown(state)

    threads = [threading.Thread(target=_worker, args=(wid,)) for wid in range(n_workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return n_workers
