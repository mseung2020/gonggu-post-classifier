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
import os
import queue
import sys
import threading
import time
from types import SimpleNamespace

from playwright.sync_api import sync_playwright

from gonggu.resolve_links.browser import LazyPage
from gonggu.resolve_links.config import MAX_BROWSERS

# 스톨 워치독(2026-08-12) — sync Playwright는 워커마다 독립 드라이버를 쓰는데, 그 드라이버(노드
# 프로세스)나 브라우저가 죽으면 page.goto의 타임아웃조차 발동 못 하고(타임아웃은 살아있는
# 드라이버가 재워줘야 함) 그 워커가 sync 호출에서 영원히 멈춘다. 굳은 워커가 MAX_BROWSERS 허가증을
# 쥔 채 멈추면 나머지 워커까지 허가증을 못 받아 풀 전체가 정지한다(실측: resolve/rescan/backfill이
# 몇 시간씩 무진척으로 멈춤, 2026-08-11~12). 죽은 드라이버에 물린 sync 호출을 다른 스레드에서
# 안전하게 깨울 방법이 없어(스레드 안전 아님) 워커 단위 되살리기는 불가 — 대신 "풀 전체가
# CRAWL_STALL_TIMEOUT초 동안 단 한 건도 진척이 없으면" 무한 정지로 판단하고 이 단계 프로세스를
# 즉시 끝낸다(os._exit). daily는 이 단계 실패를 보고 재개 힌트를 띄우고, 각 단계는 멱등이라
# `--from <단계>`로 이어서 돌리면 이미 끝난 건은 건너뛴다. 남은 락은 pid가 죽어 다음 실행의
# acquire_lock이 덮어쓴다(common.acquire_lock). 0이면 워치독을 끈다.
STALL_TIMEOUT = float(os.environ.get('CRAWL_STALL_TIMEOUT', '300'))


def _should_abort(idle, done, total, timeout):
    """풀이 멈췄다고 판단할 조건 — 타임아웃 켜짐 & 아직 남은 항목이 있는데 idle이 임계 초과.
    (테스트를 위해 순수 함수로 분리 — os._exit 경로와 로직을 떼어 검증한다.)"""
    return timeout > 0 and done < total and idle > timeout


def _stall_message(idle, done, total, warn_hint=None):
    hint = f' (동시성 {warn_hint}를 낮추면 빈도가 줄어듭니다)' if warn_hint else ''
    return (f'\n✗ 크롤 풀이 약 {idle}초간 한 건도 진척이 없습니다 ({done}/{total} 처리 후 정지) — '
            f'드라이버 먹통으로 판단하고 이 단계를 강제 종료합니다{hint}.\n'
            f'  재개: python3 -m gonggu.daily --from <이 단계>  (각 단계는 멱등이라 이미 끝난 건은 건너뜁니다)')


def _abort(msg):  # 테스트에서 monkeypatch로 대체(os._exit는 프로세스를 즉시 죽이므로).
    print(msg, file=sys.stderr, flush=True)
    print(msg, flush=True)
    os._exit(3)


def run_crawl_pool(items, handle, *, concurrency, item_delay=0.0,
                   delay_only_after_browser=False,
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
    - delay_only_after_browser: True면 이번 항목에서 실제로 브라우저를 쓴 경우에만
      item_delay를 적용한다(4단계 D1 — 패스트패스/캐시로 끝난 항목은 안 쉼, 근거는
      config.ITEM_DELAY_SMART 주석). False(기본)면 예전처럼 매 항목 대기.
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

    # 스톨 워치독용 진척 추적 — 항목 하나가 끝날 때마다(성공/실패 무관) last/done을 갱신한다.
    total_items = len(items)
    progress = {'last': time.monotonic(), 'done': 0}
    prog_lock = threading.Lock()
    stop_watchdog = threading.Event()

    def _watchdog():
        if STALL_TIMEOUT <= 0:
            return
        while not stop_watchdog.wait(min(15.0, STALL_TIMEOUT)):
            with prog_lock:
                idle = time.monotonic() - progress['last']
                done = progress['done']
            if _should_abort(idle, done, total_items, STALL_TIMEOUT):
                _abort(_stall_message(int(idle), done, total_items, warn_hint))
                return

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
                    # 진척 갱신 — 워치독이 "풀 전체 무진척"을 이걸로 판단한다(성공/실패 무관, 항목 1건 완료).
                    with prog_lock:
                        progress['last'] = time.monotonic()
                        progress['done'] += 1
                    # 브라우저를 더 안 쓰게 됐는데 기다리는 워커가 있으면 넘겨준다 — sleep 전에
                    # (어차피 자는 동안 브라우저를 붙잡고 있을 이유가 없다). 사용 여부는
                    # release가 플래그를 리셋하기 전에 읽어둔다.
                    browser_used = page.used_since_release
                    page.release_if_contended()
                    if item_delay and (browser_used or not delay_only_after_browser):
                        time.sleep(item_delay)
                page.close()
        finally:
            if worker_teardown and state is not None:
                worker_teardown(state)

    watchdog = threading.Thread(target=_watchdog, daemon=True)
    watchdog.start()
    threads = [threading.Thread(target=_worker, args=(wid,)) for wid in range(n_workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stop_watchdog.set()   # 정상 종료 — 워치독 깨워 즉시 끝냄(데몬이라 안 깨워도 무방하지만 깔끔히)
    return n_workers
