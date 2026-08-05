"""llm_batch.py — 공용 재시도 정책과 배치 러너(2단계 B2). 기존 classify 계열 3벌의
동작(429 장기 재시도 / 일반 3회 재시도 / 진행 집계)이 그대로 보존됐는지 박제한다."""
import requests

import gonggu.llm_batch as lb
from gonggu.llm_batch import MAX_RETRY, MAX_RETRY_429, retry_llm, run_llm_batch


def _err_429():
    resp = requests.Response()
    resp.status_code = 429
    return requests.exceptions.HTTPError('429 Client Error: Too Many Requests', response=resp)


class TestRetryLlm:
    def test_success_first_try(self, monkeypatch):
        monkeypatch.setattr(lb.time, 'sleep', lambda s: None)
        assert retry_llm(lambda: {'ok': 1}) == ({'ok': 1}, None)

    def test_generic_error_gives_up_after_max_retry(self, monkeypatch):
        sleeps = []
        monkeypatch.setattr(lb.time, 'sleep', sleeps.append)
        calls = []

        def boom():
            calls.append(1)
            raise ValueError('Read timed out xyz')

        parsed, err = retry_llm(boom)
        assert parsed is None and 'Read timed out' in err
        assert len(calls) == MAX_RETRY            # 총 시도 3회
        assert sleeps == [1.5, 3.0]               # 마지막 실패 후에는 안 잠

    def test_429_retries_long_then_gives_up(self, monkeypatch):
        sleeps = []
        monkeypatch.setattr(lb.time, 'sleep', sleeps.append)
        calls = []

        def boom():
            calls.append(1)
            raise _err_429()

        parsed, err = retry_llm(boom)
        assert parsed is None and '429' in err
        assert len(calls) == MAX_RETRY_429 + 1    # 첫 시도 + 재시도 10회
        assert sleeps[0] == 5 and sleeps[-1] == 50 and max(sleeps) <= 60

    def test_429_then_success(self, monkeypatch):
        monkeypatch.setattr(lb.time, 'sleep', lambda s: None)
        state = {'n': 0}

        def flaky():
            state['n'] += 1
            if state['n'] <= 2:
                raise _err_429()
            return {'v': state['n']}

        assert retry_llm(flaky) == ({'v': 3}, None)

    def test_429_does_not_consume_generic_budget(self, monkeypatch):
        """429 여러 번 + 일반 오류 2번이 섞여도 각자 별도 카운터로 관리된다."""
        monkeypatch.setattr(lb.time, 'sleep', lambda s: None)
        seq = [_err_429(), ValueError('a'), _err_429(), ValueError('b'), 'OK']
        it = iter(seq)

        def flaky():
            v = next(it)
            if isinstance(v, Exception):
                raise v
            return v

        assert retry_llm(flaky) == ('OK', None)


class TestRunLlmBatch:
    def test_counters_and_persist(self, capsys):
        persisted = []
        todo = [{'id': i} for i in range(7)]

        def process(item):
            if item['id'] % 3 == 0:
                return {**item, 'classification_error': 'boom'}
            return {**item, 'classification': {'is_gonggu': item['id'] % 2 == 0},
                    'classification_error': None}

        counters = run_llm_batch(todo, process, persisted.append, concurrency=3,
                                 ok_start=100, extra=('공구판정', lambda r: r['classification']['is_gonggu']),
                                 report_every=30)
        assert len(persisted) == 7
        assert counters['ok'] == 4 and counters['err'] == 3   # id 0,3,6 실패
        assert counters['extra'] == 2                          # id 2,4 (짝수 & 성공)
        out = capsys.readouterr().out
        assert '누적 성공 104 / 실패 3 / 공구판정 2' in out
        assert '실패 예시: boom' in out

    def test_empty_todo(self):
        counters = run_llm_batch([], lambda x: x, lambda r: None, concurrency=2)
        assert counters == {'ok': 0, 'err': 0, 'extra': 0}
