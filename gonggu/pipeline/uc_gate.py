#!/usr/bin/env python3
"""uc 신뢰 게이트(2026-08-20) — daily가 uc 단계 앞에서 통과시키는 관문.

배경: 예전에는 uc를 쓰는 날마다 손으로 이렇게 쳤다.

    rm -rf ~/.gonggu_uc_profile
    python3 -m gonggu.enrich_detail.warmup_naver_uc     # 크롬 창에서 사람이 로그인/캡차 통과

즉 **매번 신뢰를 버리고 처음부터 다시 쌓았다**. 그런데 프로필이 멀쩡한 날이 대부분이라 이건
대부분 낭비였고, 무엇보다 워밍업이 input()으로 사람을 기다려서 "한 줄 무인 실행"과 정면으로
충돌했다. 이 게이트가 그 순서를 뒤집는다:

    1) 쿠키가 살아있는지 **비대화형으로 먼저 확인**한다(uc_healthcheck.probe — 창이 잠깐 떴다
       닫힐 뿐 사람을 안 기다린다).
    2) 살아있으면 그대로 통과 — 프로필 삭제도, 워밍업도 없다(대부분의 날).
    3) 죽었으면 그때만 프로필을 초기화하고 워밍업을 띄운다.

⚠ 워밍업을 띄울지 말지는 타임아웃이 아니라 **sys.stdin.isatty()** 로 정한다. cron/nohup으로
돌면 stdin이 TTY가 아니라 입력이 영영 안 들어오므로, N분을 기다리는 건 그냥 N분을 버리는
것이다. TTY가 아니면 즉시 uc 단계를 건너뛴다. 타임아웃(UC_WARMUP_TIMEOUT_SEC)은 "TTY이긴 한데
사람이 자리를 비운" 경우에만 걸리는 2차 안전판이다.

⚠ 이 게이트는 **쿠키 신뢰만** 책임진다 — uc의 크래시 내성을 주지는 않는다(README의 2026-08-12
기록: uc는 대량 무인 경로에서 반복 크래시가 확인돼 데일리에서 뺐던 물건이다). uc 단계가 죽거나
느릴 때 데일리 전체가 묶이지 않게 하는 건 게이트가 아니라 그 단계의 시간 예산
(UC_TIME_BUDGET_SEC)과 critical=False 다.

판정은 **실행 1회당 한 번만** 하고 캐시한다 — probe()도 crawl 창을 실제로 띄우므로, uc 단계가
여러 개라고 매번 부르면 창이 그 수만큼 떴다 닫힌다.
"""
import os
import shutil
import subprocess
import sys

WARMUP_MODULE = 'gonggu.enrich_detail.warmup_naver_uc'
# TTY인데 사람이 자리를 비운 경우의 2차 안전판(초). 0이면 무기한 대기.
WARMUP_TIMEOUT_SEC = float(os.environ.get('UC_WARMUP_TIMEOUT_SEC', '600'))

# 실행 1회 안에서 공유하는 판정 캐시 — reset_cache()로 비운다(테스트/재점검용).
_CACHE = {}


def decide(trust_ok, interactive):
    """게이트가 할 일을 정하는 순수 함수 — (action, 사유).

    action: 'ok'(그대로 통과) | 'warmup'(초기화+워밍업 필요) | 'skip'(uc 단계 건너뜀)
    """
    if trust_ok:
        return 'ok', '신뢰 유효 — 워밍업 생략'
    if not interactive:
        return 'skip', '신뢰 만료인데 stdin이 TTY가 아님(무인 실행) — uc 단계를 건너뜁니다'
    return 'warmup', '신뢰 만료 — 프로필 초기화 후 워밍업 필요'


def profile_path():
    """지금 uc가 실제로 쓰는 프로필 경로. UC_PROFILE이 있으면 그게 이긴다(uc_engine과 동일 규칙).

    uc_engine을 import하면 selenium까지 딸려오므로 지연 import한다 — 게이트 판정 로직만 쓰는
    테스트가 크롬/셀레늄 없이 돌아야 한다.
    """
    override = os.environ.get('UC_PROFILE')
    if override:
        return override
    from gonggu.uc_engine import DEFAULT_PROFILE
    return DEFAULT_PROFILE


def is_safe_profile_path(path):
    """rm -rf 대상으로 안전한 경로인지 — 이름에 'uc_profile'이 들어간 홈/저장소 하위 폴더만.

    손으로 치던 `rm -rf ~/.gonggu_uc_profile`을 코드로 옮기는 것이라, UC_PROFILE에 오타나 빈
    값이 들어왔을 때 엉뚱한 곳을 지우지 않게 잠금장치를 둔다(순수 함수 — 테스트로 못박는다).
    """
    if not path:
        return False
    norm = os.path.normpath(os.path.abspath(os.path.expanduser(str(path))))
    if norm in ('/', os.path.expanduser('~'), os.path.normpath(os.path.expanduser('~'))):
        return False
    if norm.count(os.sep) < 2:      # /foo 같은 최상위 한 칸짜리 경로 거부
        return False
    return 'uc_profile' in os.path.basename(norm)


def reset_profile(path, printer=print):
    """프로필 폴더를 통째로 지운다(손으로 치던 rm -rf). 안전 검사에 걸리면 안 지우고 False."""
    if not is_safe_profile_path(path):
        printer(f'  ⚠ uc 프로필 경로가 안전 검사에 걸려 초기화를 건너뜁니다: {path!r}')
        return False
    try:
        shutil.rmtree(os.path.expanduser(str(path)))
    except FileNotFoundError:
        pass                        # 이미 없으면 초기화된 것과 같다
    except OSError as e:
        printer(f'  ⚠ uc 프로필 초기화 실패(계속 진행): {str(e)[:140]}')
        return False
    return True


def run_warmup(timeout_sec=None, printer=print):
    """워밍업을 서브프로세스로 띄우고 사람이 끝낼 때까지 기다린다 — (ok, 사유).

    stdin/stdout을 물려줘야 워밍업의 안내문과 input()이 그대로 터미널에 붙는다(daily의 다른
    단계처럼 파이프로 잡으면 사람이 프롬프트를 못 본다).
    """
    timeout = WARMUP_TIMEOUT_SEC if timeout_sec is None else timeout_sec
    printer(f'  → 워밍업 창을 띄웁니다. 뜬 크롬에서 로그인/보안확인을 통과하고 터미널에 Enter를 눌러주세요'
            f"{f' (최대 {int(timeout)}초 대기)' if timeout > 0 else ''}.")
    try:
        proc = subprocess.run([sys.executable, '-m', WARMUP_MODULE],
                              timeout=(timeout if timeout > 0 else None))
    except subprocess.TimeoutExpired:
        return False, f'워밍업 {int(timeout)}초 내 무응답(자리 비움으로 판단) — uc 단계를 건너뜁니다'
    except Exception as e:
        return False, f'워밍업 실행 실패: {str(e)[:140]}'
    if proc.returncode != 0:
        return False, f'워밍업이 exit {proc.returncode}로 끝남 — uc 단계를 건너뜁니다'
    return True, '워밍업 완료 — 프로필에 신뢰를 새로 쌓았습니다'


def reset_cache():
    _CACHE.clear()


def ensure_trust(*, force=False, printer=print, interactive=None,
                 prober=None, resetter=None, warmer=None):
    """uc 단계를 돌려도 되는지 판정하고 (ok, 사유)를 돌려준다. 실행 1회당 한 번만 실제로 확인한다.

    force=True면 캐시를 무시하고 다시 확인한다 — uc 단계가 실패한 뒤 "쿠키가 그새 만료된 건지"
    다시 볼 때만 쓴다(그 외에는 크롬 창을 아끼려고 캐시를 쓴다).
    prober/resetter/warmer는 테스트 주입용 — 기본값이 실제 동작이다.
    """
    if not force and 'ok' in _CACHE:
        return _CACHE['ok'], _CACHE['reason'] + ' (이번 실행에서 이미 확인)'

    prober = prober or _default_prober
    resetter = resetter or reset_profile
    warmer = warmer or run_warmup
    if interactive is None:
        interactive = sys.stdin is not None and sys.stdin.isatty()

    trust_ok, probe_reason = prober()
    action, reason = decide(trust_ok, interactive)
    printer(f'  uc 신뢰 점검: {probe_reason} → {reason}')

    if action == 'ok':
        ok = True
    elif action == 'skip':
        ok = False
    else:
        path = profile_path()
        printer(f'  → uc 프로필 초기화: {path}')
        resetter(path, printer=printer)
        ok, reason = warmer(printer=printer)
        printer(f'  {"✓" if ok else "⚠"} {reason}')

    _CACHE['ok'], _CACHE['reason'] = ok, reason
    return ok, reason


def profile_exists(path):
    """프로필 폴더가 실제로 있는지 — 순수 함수(테스트용)."""
    return bool(path) and os.path.isdir(os.path.expanduser(str(path)))


def _default_prober():
    """신뢰 판정 — 프로필이 아예 없으면 크롬을 안 띄우고 즉시 '만료'로 답한다.

    2026-08-20에 추가. 폴더가 없으면 쿠키도 없는 게 자명한데 예전엔 그걸 확인하려고 크롬 창을
    띄웠다(첫 실행, 또는 사람이 rm -rf 한 직후에 항상 걸리는 경로다 — 실제로 이 통합을 검증하러
    프로필 상태를 보다가 발견했다). 창 하나와 십수 초를 아끼고, 어차피 바로 뒤에 워밍업이
    새 창을 띄우므로 사용자가 보는 창 개수도 2개에서 1개로 준다.
    """
    path = profile_path()
    if not profile_exists(path):
        return False, f'프로필 없음({path}) — 크롬을 띄우지 않고 만료로 판단'
    from gonggu.uc_healthcheck import probe
    return probe()
