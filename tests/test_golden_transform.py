"""골든 파이프라인 테스트 — 대공사의 핵심 안전망.

실제 02_classified에서 뽑은 고정 샘플(fixtures/classified_sample.jsonl)을 현재 코드의
transform_one에 태운 결과가, 박제해둔 골든(fixtures/golden_transform.jsonl)과 레코드 단위로
완전히 같은지 확인한다. 리팩터링 커밋은 전부 이 테스트를 통과해야 하고, 판정 규칙을
의도적으로 바꿀 때만 tests/_make_golden.py로 골든을 갱신한다(같은 커밋에서).

날짜는 GONGGU_TODAY=2026-08-05로 고정 — transform이 이 날짜 기준으로 완전 결정론이 된다.
"""
import json
import pathlib

import pytest

FIXTURES = pathlib.Path(__file__).resolve().parent / 'fixtures'


def _load(name):
    with open(FIXTURES / name, encoding='utf-8') as f:
        return [json.loads(line) for line in f if line.strip()]


@pytest.fixture(autouse=True)
def _freeze_today(monkeypatch):
    monkeypatch.setenv('GONGGU_TODAY', '2026-08-05')


def test_transform_matches_golden():
    from gonggu.transform import transform_one

    sample = _load('classified_sample.jsonl')
    golden = _load('golden_transform.jsonl')
    assert len(sample) == len(golden)

    mismatches = []
    for i, (rec, want) in enumerate(zip(sample, golden)):
        parent, products, reject = transform_one(rec)
        got = {'parent': parent, 'products': products, 'reject': reject}
        if got != want:
            mismatches.append((i, want, got))

    assert not mismatches, (
        f'{len(mismatches)}건이 골든과 다름 — 첫 사례(index {mismatches[0][0]}):\n'
        f'  golden: {json.dumps(mismatches[0][1], ensure_ascii=False)[:400]}\n'
        f'  got   : {json.dumps(mismatches[0][2], ensure_ascii=False)[:400]}'
    )
