#!/usr/bin/env python3
"""일일 퀘스트 오케스트레이터(대공사 2단계 B6, 2026-08-05) — 손으로 치던 명령 10개를 하나로.

    python3 -m gonggu.daily              # 6→1→8-1→2→8-2→3→4→5→7→9 전체 실행(인포크 JSON은 4=resolve에 흡수)
    python3 -m gonggu.daily --list       # 실행 순서와 각 단계의 동시성 기본값만 출력
    python3 -m gonggu.daily --from resolve_links   # 이 단계부터 이어서(그 앞은 건너뜀)
    python3 -m gonggu.daily --only load            # 이 단계 하나만

순서 제약(README의 "매일 돌리는 순서" 그대로): 6(update_gonggu_stage)이 7(rescan)/9(backfill)
보다 먼저 와야 그날의 '진행중'/'판단불가' 상태가 확정된 뒤 그걸 기준으로 대상을 고를 수 있고,
4(resolve)가 5(load)보다 먼저여야 링크 해석 결과가 DB에 반영된다(load는 UPDATE 없음).

동시성 기본값은 실제 운영하던 값(CONCURRENCY=200 등)이며, 환경변수로 넘기면 그 값이 이긴다:
    CONCURRENCY=50 python3 -m gonggu.daily

출력: 각 단계의 stdout은 콘솔과 로그 파일(data/logs/daily_<시각>.log)에 같이 남고,
stderr(Playwright 노이즈 등)는 로그 파일에만 남는다 — 예전처럼 2>/tmp/...로 통째로 버리면
진짜 에러까지 안 보였다(감사 A5). 단계가 실패하면 그 단계 stderr의 마지막 줄들을 콘솔에
보여주고 중단한다(이미 끝난 단계들의 체크포인트는 그대로니, 문제 해결 후
`--from <그 단계>`로 이어서 실행).

스톨 자동 재시도(2026-08-18): resolve_links/rescan_inprogress/backfill_period는 crawl_pool의
스톨 워치독이 있어 "드라이버 먹통"이면 CRAWL_STALL_EXIT_CODE로 그 단계만 강제 종료한다(각
단계는 체크포인트라 멱등). 예전엔 이게 나면 사람이 알아채고 `--from <단계>`를 손으로 다시
쳐야 했는데(2026-08-18 실측 — 6,201건 중 1,117건 처리 후 314초 무진척으로 정지), 그 재시도
자체는 항상 안전한 조작이라 daily가 대신 몇 번 해준다 — 이 exit code로 죽은 경우에만이다
(DEEPSEEK_KEY 누락 같은 진짜 설정 오류까지 자동 재시도하면 원인을 못 보고 계속 헛돌 수 있어서
그런 경우는 여전히 즉시 중단한다). 기본 2회(STAGE_STALL_RETRIES 환경변수로 조정, 총 최대
1+N번 시도) — 그래도 안 되면 원래대로 사람이 봐야 할 진짜 문제로 보고 중단한다.

이중 실행 방지: data/output/.daily.lock에 pid를 남기고, 살아있는 프로세스가 잡고 있으면
시작을 거부한다(classify/resolve/load 동시 2회 실행 금지 제약을 코드로 강제).
"""
import collections
import contextlib
import datetime
import os
import pathlib
import subprocess
import sys
import threading
import time

from gonggu.common import CRAWL_RECYCLE_EXIT_CODE, CRAWL_STALL_EXIT_CODE, ROOT

LOG_DIR = ROOT / 'data/logs'
LOCK_FILE = ROOT / 'data/output/.daily.lock'

# 스톨(먹통) 종료로 판단해 daily가 대신 재시도해줄 최대 횟수 — 0이면 예전처럼 자동 재시도 없이
# 즉시 중단(사람이 --from으로 재개)한다. 환경변수로 넘기면 그 값이 이긴다.
#
# 기본 6(2026-08-18, 속도개선 공사 — 원래 2였음): resolve_links를 RESOLVE_CONCURRENCY=60으로
# 올리면서 스톨 빈도 자체가 늘었다(실측 — 같은 날 3연속 스톨로 재시도 2회를 다 쓰고 완전히
# 중단된 적 있음). 재시도 자체는 체크포인트라 항상 안전한 조작이고, "가끔 걸려도 결국 끝까지
# 밀어붙이는" 쪽이 지금 우선순위(빠른 처리)에 맞아 상한을 넉넉히 올려둔다.
STAGE_STALL_RETRIES = int(os.environ.get('STAGE_STALL_RETRIES', '6'))

# ⚠ uc를 데일리 대량 resolve/rescan에 상시 넣는 건 철회했다(2026-08-12). 이유: 이 맥의 최신
# 크롬(v151) + undetected_chromedriver 조합이 동시성 높은 대량 경로에서 반복 크래시("Chrome이
# 예기치 않게 종료")를 내 데일리가 정지·불안정해졌다. uc는 무겁고 단일 드라이버 직렬이라 대량
# 무인 경로엔 부적합 — 대신 데일리는 안정적인 Playwright로 돌리고, 네이버/오픈마켓 구제는 사람이
# 곁에서 낮은 동시성으로 돌리는 별도 패스(python3 -m gonggu.resolve_links.reverify_uc)로 한다.
# 다시 데일리에 uc를 켜고 싶으면 아래 resolve_links/rescan_inprogress 단계 env에
# RESOLVE_UC=1, RESOLVE_UC_HOSTS=..., UC_LOGIN_WAIT=0 을 넣으면 된다(권장하지 않음).

# 동시성 두 손잡이는 별개다(2026-08-13): 여기 값은 "워커 수"(동시 처리 상품 수)고, 실제 뜨는
# 크롬 개수는 config.MAX_BROWSERS가 따로 상한한다. 워커를 40으로 올려도 브라우저가
# 필요한 작업은 그 상한 안에서만 돌지만, LLM#2/#3 호출은 브라우저를 안 먹어서 워커↑만큼 병렬로
# 빨라진다 — 그래서 LLM 바운드 단계(resolve/rescan는 fast-skip 후, backfill_inpock는 크롤 자체가
# 없음)는 40이 이득이다. 반대로 몰 크롤=브라우저 바운드인 backfill_period는 워커를 올려봤자
# 크롬이 병목이고, 40이면 그걸 서로 뺏는 churn으로 예전처럼 얼어붙는다 — 그래서 낮게 둔다.
#
# resolve_links는 2026-08-18 속도개선 공사(A+B+C)에서 재조정했다 — 16GB 맥 기준 실측:
#   MAX_BROWSERS: 자동계산 기본값(~10)보다 14가 7.6% 빠름(67건 매칭 비교, 크래시 없음).
#     하드캡 16까지 테스트했지만 오히려 더 느렸다(스왑 압박 증가로 추정) — 14 확정, 그 이상 금지.
#   ITEM_DELAY=0, LINK_LLM_TIMEOUT=45(+재시도 1회): 안티봇 대기·LLM 꼬리 지연을 깎아 초반
#     구간 0.95초/건까지 나왔다(원래 기본 약 4.6~4.9초/건). 대신 차단율/에러율이 오르는지는
#     계속 지켜볼 것 — 리스크를 알고 켠 값이다.
#   RESOLVE_CONCURRENCY=16(2026-08-19 재조정, 예전 60): ⚠ 이 값은 Tier1(브라우저 패스) 전용이다 —
#     Tier0(브라우저 없는 빠른 패스)는 RESOLVE_FAST_CONCURRENCY(200)를 따로 쓴다(runner.py의
#     use_playwright=False 패스). 2026-08-18 티어 분리 전에는 이 값 하나가 두 종류를 다 덮어서
#     60이 의미가 있었지만, 분리 후로는 "브라우저 14개짜리 일을 워커 60개가 나눠 갖는" 초과예약만
#     남았다. 그 초과예약이 실제로 얼마나 비쌌는지가 2026-08-19에 드러났다:
#       워커는 큐에서 항목을 먼저 꺼낸 뒤(crawl_pool의 _worker_loop) handle 안에서 LazyPage가
#       브라우저 허가증을 기다린다 — 즉 동시에 "붙잡힌" 항목 수가 MAX_BROWSERS가 아니라
#       RESOLVE_CONCURRENCY다. 60이면 60건이 인질이 되고, 정기 재기동이 오면 그게 통째로 재작업이
#       된다. 실측(같은 꼬리 163건, 연속 비교):
#         RESOLVE_CONCURRENCY=60 → 240초 가동에 완료 3건 / 진행 중 60건, 드레인 90초 동안 완료 0건,
#                                  330초에 60건 폐기. 두 사이클 9분에 9건(≈1건/분).
#         RESOLVE_CONCURRENCY=16 → 7분 22초에 120건(≈16.3건/분), 재기동 0회. 약 16배.
#       스왑도 같이 내려간다 — 워커마다 Playwright 드라이버 프로세스가 하나씩 붙어서, 60개일 때
#       16GB 맥의 스왑 여유가 933MB까지 떨어졌다(2026-07-30 스왑 사고와 같은 방향).
#     MAX_BROWSERS(14)보다 살짝 위로 둬서 허가증 경합은 없애되 브라우저가 노는 순간은 메운다.
#     ⚠ 올리지 말 것 — crawl_pool이 시작 시 워커>MAX_BROWSERS*3이면 경고하는 게 바로 이 초과예약이다.
#     "한 포스트가 상품 수십 개를 공유"하는 이상치 배치의 스톨은 여전히 체크포인트로 자동 복구되고
#     (STAGE_STALL_RETRIES), 더 줄이고 싶으면 RESOLVE_SHARD_COUNT(runner.py)를 켜면 된다.
#   CRAWL_RECYCLE_SEC=900(2026-08-19 재조정, 예전 240): 240초는 위 RESOLVE_CONCURRENCY=60과 짝이
#     됐을 때 최악이었다 — 이 꼬리는 건당 수 분이 걸리는데 4분마다 끊으니 아무것도 완주하지 못하고
#     60건씩 재작업만 쌓였다(위 실측). 동시성을 16으로 내리면 인질이 16건으로 줄어 재기동 비용
#     자체가 작아지므로, 주기를 늘려 "완주할 시간"을 주는 쪽이 이득이다.
#     ⚠ 900은 아직 완전히 검증된 값이 아니다 — 2026-08-19 실측 실행은 첫 재기동(900초) 전에 물량이
#     끝나서, 15분짜리 사이클에서 원래 문제였던 메모리 열화가 어느 정도인지 못 봤다. 동시성 16이면
#     드라이버 프로세스가 44개 적어 열화도 느릴 것으로 보지만 확인은 필요하다. 대량 배치에서 후반
#     처리 속도가 눈에 띄게 떨어지면 이 값을 먼저 의심할 것(400~600 사이로 낮춰보면 된다).
#   (예전 기록) CRAWL_RECYCLE_SEC=240: 사용자가 직접 관찰 — 브라우저 풀을 오래 재사용할수록(약 5분 지나면)
#     처리 속도가 눈에 띄게 떨어지고, 껐다 켜면 다시 빨라진다(메모리 누적으로 추정, 실측된 스왑
#     사용량 증가와 일치). 4분(240초)마다 스톨이 아니어도 프로세스를 스스로 정리·재시작해 브라우저
#     풀을 새로 띄운다 — daily는 이 재시작(CRAWL_RECYCLE_EXIT_CODE)을 실패로 안 세고 무제한
#     자동으로 이어서 재개한다(crawl_pool.py 참고).
#   CRAWL_RECYCLE_DRAIN_SEC=180(2026-08-19 추가): 위 재기동이 os._exit로 즉사시키는 바람에 "큐에서
#     꺼내 처리 중이던" 항목이 체크포인트에 못 남고 통째로 재작업이 되고 있었다. 이제 재기동 시각이
#     되면 새 항목 공급만 끊고 진행 중인 건은 마치게 한 뒤 종료한다. 유예 안에 안 끝난 워커(먹통
#     드라이버)는 예전처럼 버리고 나가며, 종료 메시지가 "재작업 없음"인지 "N건 다시 처리"인지로
#     어느 쪽인지 알려준다. 0으로 두면 예전(즉시 종료) 동작.
#     처음엔 90으로 뒀다가 180으로 늘렸다 — 90초는 건당 수 분짜리 꼬리에서 한 건도 못 끝내고 유예만
#     태웠다(실측: 드레인 90초 동안 완료 0건). 동시성 16이면 마쳐야 할 in-flight도 16건뿐이라
#     유예를 늘리는 비용이 작다.
#     ⚠ 드레인만으로는 부족했다는 게 이번 교훈이다 — in-flight가 RESOLVE_CONCURRENCY만큼(60건)
#     쌓이는 구조에선 유예를 얼마로 잡아도 다 못 끝낸다. 동시성을 브라우저 수에 맞추는 것(위
#     RESOLVE_CONCURRENCY=16)이 짝으로 있어야 이 장치가 값을 한다.
# (모듈명, 이 단계 전용 동시성/기간 기본값) — 환경변수로 이미 지정돼 있으면 그 값이 이긴다.
STAGES = [
    ('update_gonggu_stage', {}),                          # 6. 공구 상태 갱신
    ('fetch_source',        {'DAYS_BACK': '7'}),          # 1. 원본 수집
    ('fetch_yt_ppl',        {'DAYS_BACK': '7'}),          # 8-1. 유튜브 PPL 원본 수집(독립)
    ('classify',            {'CONCURRENCY': '200'}),      # 2. LLM#1 공구 분류
    ('classify_yt_ppl',     {'CONCURRENCY': '200'}),      # 8-2. 유튜브 PPL 공구 판별(독립)
    ('transform',           {}),                          # 3. 보수적 게이트링
    ('resolve_links',       {'RESOLVE_CONCURRENCY': '16', 'MAX_BROWSERS': '14', 'ITEM_DELAY': '0',
                              'LINK_LLM_TIMEOUT': '45', 'LINK_LLM_TIMEOUT_RETRY': '1',
                              'CRAWL_RECYCLE_SEC': '900',
                              'CRAWL_RECYCLE_DRAIN_SEC': '180'}),  # 4. 링크 해석 — 2026-08-19 재튜닝(위 주석 참고, 약 16배)
    ('load',                {}),                          # 5. DB 적재
    # 7. 진행중 미해석 재탐색. ⚠RESCAN_CONCURRENCY는 resolve와 달리 10 — 대상이 "이미 한 번
    # 실패한 진행중 건"이라 브라우저 재검증(스토어메인/링크모음/non-uc 몰)이 몰려 브라우저
    # 바운드다(실측 2026-08-20: 처리 225건 중 브라우저 없이 끝난 건 24%뿐. resolve 첫 사이클은
    # 74%였다). 40에선 크롬10 churn으로 반복 정체(2026-08-13 실측).
    #   MAX_BROWSERS=14(2026-08-20 추가): 예전엔 이 값을 안 줘서 config의 RAM 자동계산(16GB÷1.5)이
    #     10으로 떨어졌다 — 브라우저 바운드인 이 단계가 정작 "14가 자동계산보다 7.6% 빠르다"는
    #     실측(위 resolve 주석)의 이득을 못 받고 있었다. resolve와 같은 값으로 맞춘다.
    #   CRAWL_RECYCLE_SEC/DRAIN(2026-08-20 추가): 예전엔 재기동이 꺼져 있어 수천 건을 한 프로세스로
    #     쭉 돌았고, 그만큼 "오래 재사용한 브라우저가 느려지는" 문제에 그대로 노출됐다. 안 켰던
    #     이유는 재기동 한 번에 진행 중인 건을 통째로 버렸기 때문인데, 드레인이 생긴 지금은 그
    #     대가가 1건 수준이다(실측 2026-08-20 resolve: 재기동 2회, 폐기 1건).
    ('rescan_inprogress',   {'RESCAN_CONCURRENCY': '10', 'MAX_BROWSERS': '14',
                              'CRAWL_RECYCLE_SEC': '900', 'CRAWL_RECYCLE_DRAIN_SEC': '180'}),
    # 9. 공구기간 백필 — 2026-08-18에 옛 9-0(backfill_period_inpock)과 하나로 병합(문제 10).
    # Tier0(인포크 텍스트, 브라우저 무관 — 옛 9-0의 CONCURRENCY=40 그대로 이어옴)를 먼저 돌고,
    # 거기서 못 찾은 것만 Tier1(몰 크롤=브라우저 바운드)로 넘어간다. ⚠BACKFILL_PERIOD_CONCURRENCY
    # 40 금지 — 크롬10 초과예약 churn으로 얼던 그 단계(1037에서 멈춤, backfill_period.py 참고)
    ('backfill_period',     {'PERIOD_INPOCK_CONCURRENCY': '40', 'BACKFILL_PERIOD_CONCURRENCY': '8'}),
    ('maintenance',         {}),                          # 10. 하우스키핑(컴팩션/로테이션 — 3단계 C2)
    # 인포크 허브 JSON 저장은 resolve_links 단계에서 파싱본을 그대로 떨구는 방식으로 흡수됐다
    # (2026-08-11, 중복 크롤 제거) — 별도 crawl_linkbio 단계는 데일리에서 제외. 예전에 이미 적재된
    # 포스트의 소급이 필요하면 standalone으로 `python3 -m gonggu.crawl_linkbio`를 한 번 돌린다.
]


def _acquire_lock():
    if LOCK_FILE.exists():
        try:
            pid = int(LOCK_FILE.read_text().strip())
            os.kill(pid, 0)  # 살아있으면 예외 없음
        except (ValueError, ProcessLookupError, PermissionError):
            pass  # 죽은 프로세스의 잔여 lock — 그냥 덮어쓴다
        else:
            print(f'이미 다른 일일 퀘스트가 실행 중입니다(pid {pid}, {LOCK_FILE}). '
                  f'classify/resolve/load는 동시 실행하면 안 됩니다 — 그 실행이 끝난 뒤 다시 시도하세요.',
                  file=sys.stderr)
            sys.exit(1)
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOCK_FILE.write_text(str(os.getpid()))


def _release_lock():
    try:
        LOCK_FILE.unlink()
    except FileNotFoundError:
        pass


def _run_stage(module, extra_env, log, extra_args=(), log_lock=None, tag=''):
    """한 단계를 서브프로세스로 실행 — stdout은 콘솔+로그, stderr는 로그에만.
    반환: (exit code, stderr 마지막 20줄).

    log_lock: 샤딩 실행(2026-08-18)처럼 여러 _run_stage가 같은 log 파일 객체에 동시에 쓸 때만
    넘긴다 — 단일 스레드 순차 실행(기존 동작)에서는 None이라 잠금 오버헤드가 없다.
    tag: 콘솔/로그에 붙일 접두사(예: '[샤드 0/3] ') — 샤딩 시 여러 프로세스의 출력이 섞여도
    어느 샤드인지 구분할 수 있게 한다."""
    lock_ctx = log_lock if log_lock is not None else contextlib.nullcontext()
    # PYTHONUNBUFFERED=1: 서브프로세스 stdout이 파이프로 갈 때 블록버퍼링돼 진행 로그가 한참 안
    # 보이는 문제 방지(2026-08-11 — backfill_period_inpock가 flush 없이 돌아 "멈춘 듯" 보였음).
    # 사용자 지정 환경변수가 기본값을 이기되, 언버퍼링은 항상 강제한다.
    env = {**extra_env, **os.environ, 'PYTHONUNBUFFERED': '1'}
    proc = subprocess.Popen([sys.executable, '-m', f'gonggu.{module}', *extra_args],
                            cwd=ROOT, env=env, text=True, encoding='utf-8', errors='replace',
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stderr_tail = collections.deque(maxlen=20)

    def _drain_stderr():
        for line in proc.stderr:
            stderr_tail.append(line.rstrip('\n'))
            with lock_ctx:
                log.write(f'[stderr] {tag}{line}')

    t = threading.Thread(target=_drain_stderr)
    t.start()
    for line in proc.stdout:
        with lock_ctx:
            sys.stdout.write(f'{tag}{line}')
            sys.stdout.flush()
            log.write(f'{tag}{line}')
            log.flush()
    proc.stdout.close()
    t.join()
    return proc.wait(), list(stderr_tail)


def _run_stage_with_stall_retry(module, extra_env, log, retry_limit=STAGE_STALL_RETRIES,
                                 extra_args=(), log_lock=None, tag=''):
    """_run_stage를 감싸서, 딱 CRAWL_STALL_EXIT_CODE(드라이버 먹통)로 죽었을 때만 최대
    retry_limit번 같은 단계를 다시 돈다 — 각 단계는 체크포인트라 재실행은 이미 끝난 건을
    건너뛰고 이어서 하는 것과 같다(순수 함수로 재시도 여부 판단을 분리해 실제 서브프로세스
    없이 테스트할 수 있게 한다).

    반환: (최종 exit code, 그 시도의 stderr 마지막 줄들, 전체 시도 합산 소요초, 재시도 횟수)."""
    attempt = 0
    total_dt = 0.0
    while True:
        t0 = time.monotonic()
        code, stderr_tail = _run_stage(module, extra_env, log, extra_args=extra_args,
                                        log_lock=log_lock, tag=tag)
        total_dt += time.monotonic() - t0
        if code == CRAWL_RECYCLE_EXIT_CODE:
            # 의도된 정기 재기동(2026-08-18) — 실패가 아니라 브라우저 풀을 건강하게 새로 띄우기
            # 위한 자발적 종료라, STAGE_STALL_RETRIES(진짜 먹통용 제한)를 안 쓰고 무제한 재개한다.
            # 남은 pending이 없어지면 그 실행 자체가 정상 종료(exit 0)로 끝나 이 루프를 벗어난다.
            msg = f'  ♻ {tag}gonggu.{module} 정기 재기동(exit {CRAWL_RECYCLE_EXIT_CODE}) — 체크포인트로 이어서 계속'
            print(msg, flush=True)
            with (log_lock if log_lock is not None else contextlib.nullcontext()):
                log.write(msg + '\n')
            continue
        if code != CRAWL_STALL_EXIT_CODE or attempt >= retry_limit:
            return code, stderr_tail, total_dt, attempt
        attempt += 1
        msg = (f'  ⚠ {tag}gonggu.{module} 드라이버 먹통 정지(exit {CRAWL_STALL_EXIT_CODE}) — '
               f'체크포인트로 이어서 자동 재시도 {attempt}/{retry_limit}회째')
        print(msg, flush=True)
        with (log_lock if log_lock is not None else contextlib.nullcontext()):
            log.write(msg + '\n')


def _split_evenly(total, n):
    """total을 n개로 최대한 고르게 나눈 정수 리스트(합계 total, 각 항목 최소 1) — 샤드별
    MAX_BROWSERS/RESOLVE_CONCURRENCY 배분에 쓴다. 나누지 않고 각 샤드에 원래 값을 그대로 주면
    브라우저 총수가 샤드 수배로 뛰어 오늘(2026-08-18) 실측한 RAM 안전선(MAX_BROWSERS=14)을
    그대로 넘어서므로, 반드시 전체 합이 원래 값을 넘지 않게 나눠야 한다."""
    base, extra = divmod(total, n)
    return [max(1, base + (1 if i < extra else 0)) for i in range(n)]


def _run_resolve_links_sharded(module, extra_env, log, shard_count):
    """resolve_links를 shard_count개의 독립 프로세스로 동시에 돌린다(2026-08-18, 속도개선
    다음 라운드 E) — 각 프로세스는 자기 몫의 key 파티션만 처리하고, 자기 몫 안에서 스톨이
    나면 그 샤드만(다른 샤드는 계속 진행) 최대 STAGE_STALL_RETRIES회 자동 재시도한다. "한
    이상치 포스트가 워커 60개분 전체를 볼모로 잡는" 문제(2026-08-18 실측, gonggumoa/
    V6RsKzkf7NA 3연속 스톨)의 블라스트 반경을 줄이는 게 목적.

    MAX_BROWSERS/RESOLVE_CONCURRENCY뿐 아니라 **RESOLVE_FAST_CONCURRENCY(Tier0 동시성)와
    MAX_PER_DOMAIN(도메인당 동시 접근 상한)도 전부 샤드 수만큼 나눠 배분**해 총량은 그대로
    유지한다(_split_evenly 참고, 2026-08-18 점검에서 추가 — 문제 5). 안 나누면 샤드 하나짜리
    설정값(RESOLVE_FAST_CONCURRENCY=200 등)이 샤드 수만큼 그대로 복제되어, Tier0 총
    동시요청이 샤드 수배로 뛰고 같은 도메인(스마트스토어 등)에 대한 실질 동시 접근도
    `MAX_PER_DOMAIN × 샤드수`가 되어 이 저장소가 실측 사고들로 맞춰온 안티봇 방어(429/403
    유발)가 조용히 무력화된다.
    ⚠ HOST_COOLDOWN_SEC(호스트별 차단 후 쿨다운)는 각 샤드 프로세스 메모리에만 있어서 여기서
    나눌 수 있는 값이 아니다 — 한 샤드가 어떤 호스트의 차단을 감지해도 다른 샤드는 그 사실을
    모른 채 계속 요청을 보낼 수 있다는 한계가 남는다(프로세스 간 공유 저장소가 필요한 별도
    작업 — 지금은 위 두 값을 나누는 것만으로 "동시 접근량 자체가 샤드 수배가 되는" 가장 큰
    위험은 없앤다).

    전 샤드가 성공하면 `--finalize`를 한 번 더 돌려 RESOLUTION_FILE(모든 샤드가 이미 append
    완료)을 근거로 RESOLVED_DIR을 재조립한다 — 샤드 각자가 finalize를 부르면 서로의 결과를
    모른 채 덮어써서 최종 산출물에서 다른 샤드 몫이 누락된다(runner.finalize() docstring 참고).

    반환: (최종 exit code, 실패/finalize의 stderr 마지막 줄들, 총 소요초, 전 샤드 재시도 합)."""
    merged_env = {**extra_env, **os.environ}
    browsers = _split_evenly(int(merged_env.get('MAX_BROWSERS', '14')), shard_count)
    workers = _split_evenly(int(merged_env.get('RESOLVE_CONCURRENCY', '60')), shard_count)
    fast_workers = _split_evenly(int(merged_env.get('RESOLVE_FAST_CONCURRENCY', '200')), shard_count)
    per_domain = _split_evenly(int(merged_env.get('MAX_PER_DOMAIN', '4')), shard_count)
    log_lock = threading.Lock()
    results = [None] * shard_count

    def _run_one(i):
        shard_env = {**extra_env, 'RESOLVE_SHARD_COUNT': str(shard_count), 'RESOLVE_SHARD_INDEX': str(i),
                     'MAX_BROWSERS': str(browsers[i]), 'RESOLVE_CONCURRENCY': str(workers[i]),
                     'RESOLVE_FAST_CONCURRENCY': str(fast_workers[i]), 'MAX_PER_DOMAIN': str(per_domain[i])}
        results[i] = _run_stage_with_stall_retry(module, shard_env, log, log_lock=log_lock,
                                                  tag=f'[샤드{i}] ')

    threads = [threading.Thread(target=_run_one, args=(i,)) for i in range(shard_count)]
    t0 = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    total_dt = time.monotonic() - t0

    retries_sum = sum(r[3] for r in results)
    failed = [(i, r) for i, r in enumerate(results) if r[0] != 0]
    if failed:
        i, (code, stderr_tail, _, _) = failed[0]
        note = [f'[샤드{i}이(가) 실패해 --finalize를 건너뜁니다]'] + list(stderr_tail)
        return code, note, total_dt, retries_sum

    fin_code, fin_stderr = _run_stage(module, extra_env, log, extra_args=('--finalize',),
                                       log_lock=log_lock, tag='[finalize] ')
    return fin_code, fin_stderr, total_dt, retries_sum


def main():
    argv = sys.argv[1:]
    if '--list' in argv:
        for module, defaults in STAGES:
            env_txt = ' '.join(f'{k}={v}' for k, v in defaults.items())
            print(f'  python3 -m gonggu.{module}' + (f'   (기본 {env_txt})' if env_txt else ''))
        return

    stages = STAGES
    if '--from' in argv:
        name = argv[argv.index('--from') + 1]
        idx = [i for i, (m, _) in enumerate(STAGES) if m == name]
        if not idx:
            sys.exit(f'--from: 모듈 이름이 아님: {name} (--list로 확인)')
        stages = STAGES[idx[0]:]
    if '--only' in argv:
        name = argv[argv.index('--only') + 1]
        stages = [(m, d) for m, d in STAGES if m == name]
        if not stages:
            sys.exit(f'--only: 모듈 이름이 아님: {name} (--list로 확인)')

    _acquire_lock()
    started = datetime.datetime.now()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f'daily_{started.strftime("%Y-%m-%d_%H%M%S")}.log'
    durations = []
    try:
        with open(log_path, 'w', encoding='utf-8') as log:
            print(f'일일 퀘스트 시작 — {len(stages)}단계, 로그: {log_path}')
            for module, defaults in stages:
                header = f'\n=== gonggu.{module} ==='
                print(header)
                log.write(header + '\n')
                shard_count = int({**defaults, **os.environ}.get('RESOLVE_SHARD_COUNT', '1'))
                if module == 'resolve_links' and shard_count > 1:
                    # 2026-08-18, 속도개선 다음 라운드 E — RESOLVE_SHARD_COUNT를 명시적으로 넘긴
                    # 경우에만 옵트인(기본은 예전처럼 단일 프로세스). 스톨의 블라스트 반경을
                    # 샤드 하나로 좁히는 게 목적 — _run_resolve_links_sharded docstring 참고.
                    print(f'  ⚙ resolve_links를 {shard_count}개 프로세스로 샤딩해서 동시 실행합니다.')
                    code, stderr_tail, dt, retries = _run_resolve_links_sharded(module, defaults, log, shard_count)
                else:
                    code, stderr_tail, dt, retries = _run_stage_with_stall_retry(module, defaults, log)
                durations.append((module, dt, code, retries))
                if code != 0:
                    retried_note = f' — 자동재시도 {retries}회 후에도 실패' if retries else ''
                    print(f'\n✗ gonggu.{module} 실패 (exit {code}, {dt:.0f}초){retried_note} — 중단합니다.',
                          file=sys.stderr)
                    if stderr_tail:
                        print('  stderr 마지막 출력:', file=sys.stderr)
                        for line in stderr_tail:
                            print(f'    {line}', file=sys.stderr)
                    print(f'  전체 로그: {log_path}\n  문제 해결 후: python3 -m gonggu.daily --from {module}',
                          file=sys.stderr)
                    sys.exit(code)

            summary = ['\n=== 일일 퀘스트 요약 ===']
            for module, dt, _, retries in durations:
                retried_note = f' (자동재시도 {retries}회)' if retries else ''
                summary.append(f'  {module:<22} {dt:7.0f}초{retried_note}')
            summary.append(f'  총 소요 {sum(d for _, d, _, _ in durations):.0f}초')
            text = '\n'.join(summary)
            print(text)
            log.write(text + '\n')
        # LLM 토큰 사용량(오늘)을 마지막에 붙여준다 — 실패해도 퀘스트 자체는 성공으로 둔다.
        subprocess.run([sys.executable, '-m', 'gonggu.llm_usage_report'], cwd=ROOT)
    finally:
        _release_lock()


if __name__ == '__main__':
    main()
