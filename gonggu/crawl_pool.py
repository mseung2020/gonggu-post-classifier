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

from gonggu.common import CRAWL_RECYCLE_EXIT_CODE, CRAWL_STALL_EXIT_CODE
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

# 정기 재기동(2026-08-18, 속도개선 공사 실측 근거) — 사용자가 직접 관찰: 오래 켜둔 브라우저 풀이
# 5분쯤 지나면 처리 속도가 눈에 띄게 떨어지고, 껐다 켜면(브라우저를 전부 새로 띄우면) 다시
# 빨라진다. 워커가 브라우저를 재사용(LazyPage, 재기동 3.9초를 아끼려고)하는 설계 자체가, 오래
# 살아남는 브라우저 프로세스일수록 메모리를 누적하는 대가를 같이 지고 있는 것으로 보인다(실측
# 확인된 스왑 사용량 증가와 일치). CRAWL_RECYCLE_SEC초가 지나면(아직 남은 항목이 있을 때만)
# 스톨이 아니어도 "건강한 정기 재시작"으로 프로세스를 끝낸다 — daily가 이 exit code는 실패로
# 안 세고 무제한 자동 재개한다(common.CRAWL_RECYCLE_EXIT_CODE 참고). 0(기본)이면 꺼짐 — 옵트인.
RECYCLE_AFTER_SEC = float(os.environ.get('CRAWL_RECYCLE_SEC', '0'))


def _should_abort(idle, done, total, timeout):
    """풀이 멈췄다고 판단할 조건 — 타임아웃 켜짐 & 아직 남은 항목이 있는데 idle이 임계 초과.
    (테스트를 위해 순수 함수로 분리 — os._exit 경로와 로직을 떼어 검증한다.)"""
    return timeout > 0 and done < total and idle > timeout


def _should_recycle(elapsed, done, total, recycle_after):
    """정기 재기동 조건 — 켜져 있고(recycle_after>0), 아직 남은 항목이 있는데(done<total, 다
    끝났으면 재기동할 이유 없음) 프로세스 수명이 임계를 넘었을 때. _should_abort와 대칭되는
    순수 함수(테스트 용이성)."""
    return recycle_after > 0 and done < total and elapsed > recycle_after


def _stall_message(idle, done, total, warn_hint=None):
    hint = f' (동시성 {warn_hint}를 낮추면 빈도가 줄어듭니다)' if warn_hint else ''
    return (f'\n✗ 크롤 풀이 약 {idle}초간 한 건도 진척이 없습니다 ({done}/{total} 처리 후 정지) — '
            f'드라이버 먹통으로 판단하고 이 단계를 강제 종료합니다{hint}.\n'
            f'  재개: python3 -m gonggu.daily --from <이 단계>  (각 단계는 멱등이라 이미 끝난 건은 건너뜁니다)')


def _recycle_message(elapsed, done, total):
    return (f'\n♻ 브라우저 풀을 정기적으로 재기동합니다(가동 약 {elapsed}초, {done}/{total} 처리 후) — '
            f'오래 재사용된 브라우저의 메모리 누적을 정리하기 위한 의도된 재시작이며 실패가 아닙니다.\n'
            f'  재개: python3 -m gonggu.daily --from <이 단계>  (각 단계는 멱등이라 이미 끝난 건은 건너뜁니다)')


def _abort(msg):  # 테스트에서 monkeypatch로 대체(os._exit는 프로세스를 즉시 죽이므로).
    print(msg, file=sys.stderr, flush=True)
    print(msg, flush=True)
    os._exit(CRAWL_STALL_EXIT_CODE)  # daily.py의 자동 재시도가 이 코드로만 "먹통"을 식별한다


def _recycle(msg):  # 테스트에서 monkeypatch로 대체(os._exit는 프로세스를 즉시 죽이므로).
    print(msg, file=sys.stderr, flush=True)
    print(msg, flush=True)
    os._exit(CRAWL_RECYCLE_EXIT_CODE)  # daily.py가 이 코드는 실패로 안 세고 무제한 재개한다


def run_crawl_pool(items, handle, *, concurrency, item_delay=0.0,
                   delay_only_after_browser=False,
                   worker_setup=None, worker_teardown=None,
                   save_auth_first_worker=True, warn_hint=None, use_playwright=True):
    """items를 워커 스레드들이 나눠 handle로 처리한다.

    - handle(ctx, item): 항목 하나의 처리 전부(크롤링/판단/저장/로그). ctx 속성:
        ctx.page      : LazyPage (그냥 Page처럼 쓰면 됨 — 첫 사용 때 브라우저가 뜬다) 또는
                        use_playwright=False면 None(아래 참고).
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
    - use_playwright: False면 워커가 sync_playwright()/LazyPage를 아예 안 만들고 ctx.page=None으로
      돈다(2026-08-18, 속도개선 공사 F단계 — resolve_links의 "브라우저 없는 빠른 패스"용). handle이
      정말로 브라우저를 전혀 안 쓴다고 확신할 때만 켜라 — Playwright 드라이버 프로세스 자체를
      워커 수만큼 띄우는 비용(수십~수백 ms + 메모리)까지 없애야 동시성을 수백까지 올려도 그 비용이
      공짜다. handle이 실수로 page를 건드리면 None 참조라 그 항목만 예외로 건너뛰고(위 handle
      예외 계약 그대로) 워커 풀 전체가 죽지는 않는다 — 그래도 그런 실수는 버그이니 로그를 봐야 한다.
      ⚠ use_playwright=False면 CRAWL_RECYCLE_SEC(정기 재기동)도 자동으로 무시한다(2026-08-18
      점검에서 발견 — 재기동은 "오래 재사용된 브라우저의 메모리 누적"을 정리하기 위한 것인데,
      브라우저 자체를 안 띄우는 이 모드에 그대로 적용되면 브라우저가 하나도 없는데도 "브라우저
      풀 재기동" 명분으로 프로세스를 반복 강제종료하게 된다 — resolve_links의 Tier0(빠른 패스)가
      물량이 많은 날 CRAWL_RECYCLE_SEC를 못 끝내고 죽었다 되살아나길 반복하며 Tier1(진짜 브라우저가
      필요한 단계) 진입 자체가 지연되는 사고로 이어질 수 있었다. STALL_TIMEOUT(진짜 먹통 감지)은
      브라우저 유무와 무관하게 여전히 유효하므로 그대로 둔다.

    반환: 실제 워커 수."""
    n_workers = max(1, min(concurrency, len(items) or 1))
    if warn_hint and use_playwright and n_workers > MAX_BROWSERS * 3:
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
    pool_started = time.monotonic()
    # use_playwright=False면 애초에 브라우저가 하나도 없으니 "브라우저 풀 재기동"은 의미가
    # 없다 — 무시한다(위 use_playwright docstring 참고). STALL_TIMEOUT은 브라우저 유무와
    # 무관하게(진짜 먹통 감지) 그대로 유효.
    effective_recycle = RECYCLE_AFTER_SEC if use_playwright else 0.0

    def _watchdog():
        if STALL_TIMEOUT <= 0 and effective_recycle <= 0:
            return
        # 둘 다 켜져 있으면 더 짧은 주기로 깨어나 어느 쪽이든 임계를 넘는 즉시 반응한다.
        candidates = [t for t in (STALL_TIMEOUT, effective_recycle, 15.0) if t > 0]
        poll = min(candidates) if candidates else 15.0
        while not stop_watchdog.wait(poll):
            with prog_lock:
                idle = time.monotonic() - progress['last']
                done = progress['done']
            if _should_abort(idle, done, total_items, STALL_TIMEOUT):
                _abort(_stall_message(int(idle), done, total_items, warn_hint))
                return
            elapsed = time.monotonic() - pool_started
            if _should_recycle(elapsed, done, total_items, effective_recycle):
                _recycle(_recycle_message(int(elapsed), done, total_items))
                return

    def _worker_loop(wid, page, state):
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
            if page is not None:
                # 브라우저를 더 안 쓰게 됐는데 기다리는 워커가 있으면 넘겨준다 — sleep 전에
                # (어차피 자는 동안 브라우저를 붙잡고 있을 이유가 없다). 사용 여부는
                # release가 플래그를 리셋하기 전에 읽어둔다.
                browser_used = page.used_since_release
                page.release_if_contended()
                if item_delay and (browser_used or not delay_only_after_browser):
                    time.sleep(item_delay)
            elif item_delay and not delay_only_after_browser:
                # use_playwright=False라 page 자체가 없다 — "브라우저를 썼는지"는 물을 수
                # 없으니(애초에 못 씀) delay_only_after_browser=True면 이 패스는 대기가 전혀
                # 없다는 뜻이고, False로 강제 켰다면(드문 케이스) 매 항목 대기.
                time.sleep(item_delay)
        if page is not None:
            page.close()

    def _worker(wid):
        state = worker_setup() if worker_setup else None
        try:
            if use_playwright:
                with sync_playwright() as pw:
                    page = LazyPage(pw, save_auth_state=(save_auth_first_worker and wid == 0))
                    _worker_loop(wid, page, state)
            else:
                _worker_loop(wid, None, state)
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
