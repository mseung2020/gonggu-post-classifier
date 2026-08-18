"""daily.py의 스톨 자동 재시도(2026-08-18) 계약: crawl_pool의 CRAWL_STALL_EXIT_CODE로 죽은
단계만 --from 없이 daily가 대신 몇 번 재시도하고, 그 외 실패(설정 오류 등)는 예전처럼 즉시
멈춘다. 실제 서브프로세스 없이 _run_stage를 페이크로 바꿔 재시도 판단 로직만 검증한다."""
import io

import gonggu.daily as daily
from gonggu.common import CRAWL_RECYCLE_EXIT_CODE, CRAWL_STALL_EXIT_CODE


class _FakeLog(io.StringIO):
    """log.write()만 쓰이므로 StringIO로 충분 — 나중에 내용도 확인할 수 있게 남겨둔다."""


def _run_with_codes(monkeypatch, codes, retry_limit, durations=None):
    """_run_stage가 순서대로 codes를 돌려주게 페이크로 바꾸고 결과를 반환한다."""
    calls = []
    durations = durations or [1.0] * len(codes)
    # time.monotonic()을 호출 시퀀스에 맞춰 t0, t0+dt, t0, t0+dt ... 로 흘러가게 한다.
    ticks = []
    t = 0.0
    for dt in durations:
        ticks.append(t)
        t += dt
        ticks.append(t)
    monkeypatch.setattr(daily.time, 'monotonic', lambda: ticks.pop(0))

    def fake_run_stage(module, extra_env, log, extra_args=(), log_lock=None, tag=''):
        calls.append(module)
        idx = len(calls) - 1
        return codes[idx], [f'stderr-{idx}']

    monkeypatch.setattr(daily, '_run_stage', fake_run_stage)
    log = _FakeLog()
    result = daily._run_stage_with_stall_retry('resolve_links', {}, log, retry_limit=retry_limit)
    return result, calls, log.getvalue()


class TestStallRetry:
    def test_immediate_success_no_retry(self, monkeypatch):
        (code, stderr_tail, dt, attempt), calls, log_text = _run_with_codes(
            monkeypatch, [0], retry_limit=2)
        assert code == 0 and attempt == 0 and len(calls) == 1
        assert stderr_tail == ['stderr-0']
        assert dt == 1.0
        assert '재시도' not in log_text

    def test_stall_then_success_retries_once(self, monkeypatch):
        (code, stderr_tail, dt, attempt), calls, log_text = _run_with_codes(
            monkeypatch, [CRAWL_STALL_EXIT_CODE, 0], retry_limit=2, durations=[2.0, 3.0])
        assert code == 0 and attempt == 1 and len(calls) == 2
        assert dt == 5.0  # 두 시도 합산
        assert '자동 재시도 1/2회째' in log_text

    def test_stall_exhausts_retry_limit_stays_failed(self, monkeypatch):
        codes = [CRAWL_STALL_EXIT_CODE] * 3  # 재시도 2회 다 스톨 -> 총 3번 시도
        (code, stderr_tail, dt, attempt), calls, log_text = _run_with_codes(
            monkeypatch, codes, retry_limit=2)
        assert code == CRAWL_STALL_EXIT_CODE and attempt == 2 and len(calls) == 3
        assert '자동 재시도 1/2회째' in log_text and '자동 재시도 2/2회째' in log_text

    def test_non_stall_failure_never_retried(self, monkeypatch):
        """DEEPSEEK_KEY 누락 같은 진짜 설정 오류(exit 1)는 재시도 대상이 아니다 — 원인을
        못 보고 계속 헛돌게 하면 안 되므로 즉시 실패로 반환해야 한다."""
        (code, stderr_tail, dt, attempt), calls, log_text = _run_with_codes(
            monkeypatch, [1], retry_limit=2)
        assert code == 1 and attempt == 0 and len(calls) == 1
        assert '재시도' not in log_text

    def test_retry_limit_zero_disables_retry(self, monkeypatch):
        """STAGE_STALL_RETRIES=0이면 예전 동작(자동 재시도 없음)과 완전히 같아야 한다."""
        (code, stderr_tail, dt, attempt), calls, log_text = _run_with_codes(
            monkeypatch, [CRAWL_STALL_EXIT_CODE], retry_limit=0)
        assert code == CRAWL_STALL_EXIT_CODE and attempt == 0 and len(calls) == 1
        assert '재시도' not in log_text


class TestRecycleRetry:
    """정기 재기동(exit 4, 2026-08-18)은 실패가 아니라 의도된 브라우저 풀 재시작이라, 진짜 먹통용
    STAGE_STALL_RETRIES 한도와 무관하게 무제한 재개해야 한다(안 그러면 몇 시간짜리 백로그가 4분
    (CRAWL_RECYCLE_SEC=240)마다 재기동하다 재시도 한도(기본 6회 = 24분)에 걸려 파이프라인 전체가
    중단되는 사고가 난다)."""

    def test_recycle_does_not_count_against_retry_limit(self, monkeypatch):
        codes = [CRAWL_RECYCLE_EXIT_CODE] * 5 + [0]  # 재기동 5번 겪고 성공 — retry_limit=1보다 훨씬 많음
        (code, stderr_tail, dt, attempt), calls, log_text = _run_with_codes(
            monkeypatch, codes, retry_limit=1, durations=[1.0] * 6)
        assert code == 0
        assert len(calls) == 6  # 전부 실제로 실행됨(무제한 재개)
        assert attempt == 0  # 재기동은 "재시도 횟수"(스톨 전용 카운터)에 안 잡힘
        assert log_text.count('정기 재기동') == 5

    def test_recycle_then_real_stall_still_respects_stall_retry_limit(self, monkeypatch):
        """재기동 뒤에 진짜 스톨이 나면, 그건 여전히 retry_limit이 그대로 적용돼야 한다 — 재기동이
        스톨 재시도 한도를 흐리면 안 된다."""
        codes = [CRAWL_RECYCLE_EXIT_CODE, CRAWL_STALL_EXIT_CODE, CRAWL_STALL_EXIT_CODE]
        (code, stderr_tail, dt, attempt), calls, log_text = _run_with_codes(
            monkeypatch, codes, retry_limit=1, durations=[1.0] * 3)
        assert code == CRAWL_STALL_EXIT_CODE
        assert attempt == 1  # 스톨 재시도만 카운트(한도 1 도달해 중단)
        assert len(calls) == 3
