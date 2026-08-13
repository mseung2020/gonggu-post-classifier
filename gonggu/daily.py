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

이중 실행 방지: data/output/.daily.lock에 pid를 남기고, 살아있는 프로세스가 잡고 있으면
시작을 거부한다(classify/resolve/load 동시 2회 실행 금지 제약을 코드로 강제).
"""
import collections
import datetime
import os
import pathlib
import subprocess
import sys
import threading
import time

from gonggu.common import ROOT

LOG_DIR = ROOT / 'data/logs'
LOCK_FILE = ROOT / 'data/output/.daily.lock'

# ⚠ uc를 데일리 대량 resolve/rescan에 상시 넣는 건 철회했다(2026-08-12). 이유: 이 맥의 최신
# 크롬(v151) + undetected_chromedriver 조합이 동시성 높은 대량 경로에서 반복 크래시("Chrome이
# 예기치 않게 종료")를 내 데일리가 정지·불안정해졌다. uc는 무겁고 단일 드라이버 직렬이라 대량
# 무인 경로엔 부적합 — 대신 데일리는 안정적인 Playwright로 돌리고, 네이버/오픈마켓 구제는 사람이
# 곁에서 낮은 동시성으로 돌리는 별도 패스(python3 -m gonggu.resolve_links.reverify_uc)로 한다.
# 다시 데일리에 uc를 켜고 싶으면 아래 resolve_links/rescan_inprogress 단계 env에
# RESOLVE_UC=1, RESOLVE_UC_HOSTS=..., UC_LOGIN_WAIT=0 을 넣으면 된다(권장하지 않음).

# 동시성 두 손잡이는 별개다(2026-08-13): 여기 값은 "워커 수"(동시 처리 상품 수)고, 실제 뜨는
# 크롬 개수는 config.MAX_BROWSERS(RAM 기준 ~10)가 따로 상한한다. 워커를 40으로 올려도 브라우저가
# 필요한 작업은 크롬 10개 안에서만 돌지만, LLM#2/#3 호출은 브라우저를 안 먹어서 워커↑만큼 병렬로
# 빨라진다 — 그래서 LLM 바운드 단계(resolve/rescan는 fast-skip 후, backfill_inpock는 크롤 자체가
# 없음)는 40이 이득이다. 반대로 몰 크롤=브라우저 바운드인 backfill_period는 워커를 올려봤자
# 크롬 10개가 병목이고, 40이면 그 10개를 서로 뺏는 churn으로 예전처럼 얼어붙는다 — 그래서 낮게 둔다.
# MAX_BROWSERS 자체를 올리는 건 16GB 맥에서 스왑→먹통 사고가 났던 값이라 손대지 않는다.
# (모듈명, 이 단계 전용 동시성/기간 기본값) — 환경변수로 이미 지정돼 있으면 그 값이 이긴다.
STAGES = [
    ('update_gonggu_stage', {}),                          # 6. 공구 상태 갱신
    ('fetch_source',        {'DAYS_BACK': '7'}),          # 1. 원본 수집
    ('fetch_yt_ppl',        {'DAYS_BACK': '7'}),          # 8-1. 유튜브 PPL 원본 수집(독립)
    ('classify',            {'CONCURRENCY': '200'}),      # 2. LLM#1 공구 분류
    ('classify_yt_ppl',     {'CONCURRENCY': '200'}),      # 8-2. 유튜브 PPL 공구 판별(독립)
    ('transform',           {}),                          # 3. 보수적 게이트링
    ('resolve_links',       {'RESOLVE_CONCURRENCY': '40'}),   # 4. 링크 해석(Playwright + fast-skip). 워커40/크롬10 — 남은 일 대부분이 LLM#2/#3 호출(브라우저 무관)이라 워커↑가 이득
    ('load',                {}),                          # 5. DB 적재
    ('rescan_inprogress',   {'RESCAN_CONCURRENCY': '40'}),    # 7. 진행중 미해석 재탐색(resolve와 같은 엔진 — fast-skip 적용, 동일 이유로 40)
    ('backfill_period_inpock', {'CONCURRENCY': '40'}),         # 9-0. 기간 백필(인포크, 크롤 없이 LLM만 — 브라우저 무관이라 높여도 안전)
    ('backfill_period',     {'BACKFILL_PERIOD_CONCURRENCY': '8'}),  # 9. 공구기간 백필(몰 크롤=브라우저 바운드). ⚠40 금지 — 크롬10 초과예약 churn으로 얼던 그 단계(1037에서 멈춤). fast-skip도 아직 없음
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


def _run_stage(module, extra_env, log):
    """한 단계를 서브프로세스로 실행 — stdout은 콘솔+로그, stderr는 로그에만.
    반환: (exit code, stderr 마지막 20줄)."""
    # PYTHONUNBUFFERED=1: 서브프로세스 stdout이 파이프로 갈 때 블록버퍼링돼 진행 로그가 한참 안
    # 보이는 문제 방지(2026-08-11 — backfill_period_inpock가 flush 없이 돌아 "멈춘 듯" 보였음).
    # 사용자 지정 환경변수가 기본값을 이기되, 언버퍼링은 항상 강제한다.
    env = {**extra_env, **os.environ, 'PYTHONUNBUFFERED': '1'}
    proc = subprocess.Popen([sys.executable, '-m', f'gonggu.{module}'],
                            cwd=ROOT, env=env, text=True, encoding='utf-8', errors='replace',
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stderr_tail = collections.deque(maxlen=20)

    def _drain_stderr():
        for line in proc.stderr:
            stderr_tail.append(line.rstrip('\n'))
            log.write(f'[stderr] {line}')

    t = threading.Thread(target=_drain_stderr)
    t.start()
    for line in proc.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        log.write(line)
        log.flush()
    proc.stdout.close()
    t.join()
    return proc.wait(), list(stderr_tail)


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
                t0 = time.monotonic()
                code, stderr_tail = _run_stage(module, defaults, log)
                dt = time.monotonic() - t0
                durations.append((module, dt, code))
                if code != 0:
                    print(f'\n✗ gonggu.{module} 실패 (exit {code}, {dt:.0f}초) — 중단합니다.',
                          file=sys.stderr)
                    if stderr_tail:
                        print('  stderr 마지막 출력:', file=sys.stderr)
                        for line in stderr_tail:
                            print(f'    {line}', file=sys.stderr)
                    print(f'  전체 로그: {log_path}\n  문제 해결 후: python3 -m gonggu.daily --from {module}',
                          file=sys.stderr)
                    sys.exit(code)

            summary = ['\n=== 일일 퀘스트 요약 ===']
            for module, dt, _ in durations:
                summary.append(f'  {module:<22} {dt:7.0f}초')
            summary.append(f'  총 소요 {sum(d for _, d, _ in durations):.0f}초')
            text = '\n'.join(summary)
            print(text)
            log.write(text + '\n')
        # LLM 토큰 사용량(오늘)을 마지막에 붙여준다 — 실패해도 퀘스트 자체는 성공으로 둔다.
        subprocess.run([sys.executable, '-m', 'gonggu.llm_usage_report'], cwd=ROOT)
    finally:
        _release_lock()


if __name__ == '__main__':
    main()
