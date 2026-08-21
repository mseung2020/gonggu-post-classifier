#!/usr/bin/env python3
"""골든 픽스처 생성기(1회성 + 갱신용) — 실제 02_classified 날짜 파일들에서 결정론적(seed 고정)
샘플을 뽑아 tests/fixtures/classified_sample.jsonl로 저장하고, 그 샘플을 **현재 코드**의
transform_one에 태운 결과를 tests/fixtures/golden_transform.jsonl로 박제한다.

이후 모든 리팩터링은 test_golden_transform.py가 이 두 파일로 "같은 입력 → 같은 출력"을
검증한다. 판정 규칙을 *의도적으로* 바꾸는 날에만 이 스크립트를 다시 돌려 골든을 갱신할 것
(그 커밋에는 규칙 변경과 골든 갱신이 같이 들어가야 리뷰가 된다).

사용법(저장소 루트에서):
    python3 tests/_make_golden.py data/02_classified/2026-08-03.jsonl [추가파일...]
    python3 tests/_make_golden.py --regold      # 입력 샘플은 그대로 두고 골든만 다시 계산

--regold을 쓰는 이유(2026-08-21 추가): 판정 규칙이 아니라 **출력 스키마**만 바뀌는 변경
(예: parent에 description 컬럼 추가)에서도 골든은 당연히 깨진다. 이때 인자 없는 모드로
전체 재생성하면 입력 샘플까지 최신 02 데이터로 갈려서, 커밋 diff에 "스키마 변경분"과
"샘플이 바뀐 분"이 섞여 리뷰가 불가능해진다. --regold은 classified_sample.jsonl을 건드리지
않으므로 diff가 딱 스키마 변경분만 남는다 — 규칙/스키마 변경 시의 기본 경로로 쓸 것.
"""
import json
import pathlib
import random
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import os

os.environ['GONGGU_TODAY'] = '2026-08-05'  # 골든은 항상 이 날짜 기준으로 계산/검증한다
from gonggu.transform import transform_one  # noqa: E402

FIXTURES = pathlib.Path(__file__).resolve().parent / 'fixtures'
SAMPLE_N = 400
SEED = 20260805


def _write_golden(sample):
    with open(FIXTURES / 'golden_transform.jsonl', 'w', encoding='utf-8') as f:
        for r in sample:
            parent, products, reject = transform_one(r)
            f.write(json.dumps({'parent': parent, 'products': products, 'reject': reject},
                               ensure_ascii=False) + '\n')


def regold():
    """입력 샘플은 그대로 두고 골든만 현재 코드로 다시 계산한다(위 docstring 참고)."""
    path = FIXTURES / 'classified_sample.jsonl'
    with open(path, encoding='utf-8') as f:
        sample = [json.loads(line) for line in f if line.strip()]
    _write_golden(sample)
    print(f'--regold: {path.name} {len(sample)}건 그대로 두고 golden_transform.jsonl만 재계산')


def main(paths):
    records = []
    for p in paths:
        with open(p, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
    rng = random.Random(SEED)
    sample = rng.sample(records, min(SAMPLE_N, len(records)))

    # 다양성 보장: 분류 실패/비공구/공구를 골고루 — 실패건이 샘플에 하나도 없으면 억지로 몇 개 추가
    def _bucket(r):
        if r.get('classification_error') or not r.get('classification'):
            return 'error'
        return 'gonggu' if r['classification'].get('is_gonggu') else 'not_gonggu'

    have = {_bucket(r) for r in sample}
    for want in ('error', 'gonggu', 'not_gonggu'):
        if want not in have:
            extra = [r for r in records if _bucket(r) == want][:5]
            sample.extend(extra)

    FIXTURES.mkdir(parents=True, exist_ok=True)
    with open(FIXTURES / 'classified_sample.jsonl', 'w', encoding='utf-8') as f:
        for r in sample:
            f.write(json.dumps(r, ensure_ascii=False) + '\n')

    _write_golden(sample)

    from collections import Counter
    print(f'샘플 {len(sample)}건 저장 — 버킷 분포: {Counter(_bucket(r) for r in sample)}')


if __name__ == '__main__':
    if '--regold' in sys.argv:
        regold()
    else:
        main(sys.argv[1:])
