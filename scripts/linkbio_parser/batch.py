"""URL 목록을 일괄로 parse -> save하는 수동 테스트/진단용 실행기(메인 파이프라인은 안 씀)."""
from __future__ import annotations

import os
import random
import time

from .common import OUTPUT_DIR
from .registry import fetch_raw, parse, save


def collect_urls(args: list[str]) -> list[str]:
    """.txt 파일 경로가 주어지면 줄 단위로 읽고, 아니면 인자 자체를 URL 목록으로 취급한다."""
    if len(args) == 1 and args[0].endswith(".txt") and os.path.isfile(args[0]):
        with open(args[0], encoding="utf-8") as f:
            return [line.strip() for line in f if line.strip() and not line.startswith("#")]
    return args


def run_batch(urls: list[str], delay_range: tuple[float, float] = (1.5, 3.5), save_raw: bool = False) -> None:
    """URL 목록을 하나씩 parse -> save하고, 성공/실패를 집계해 마지막에 요약 출력한다.
    각 URL 사이에 무작위 시간만큼 쉬어(delay_range) 상대 서버에 부담을 주지 않는다."""
    ok, failed = [], []

    for i, url in enumerate(urls, 1):
        print(f"[{i}/{len(urls)}] {url}")
        try:
            data = parse(url)
            out_path = save(data)
            ok.append(url)
            print(f"  -> saved: {out_path} (links: {len(data['links'])})")
            if save_raw:
                raw_path = save(fetch_raw(url), out_dir=os.path.join(OUTPUT_DIR, "raw"))
                print(f"  -> raw saved: {raw_path}")
        except Exception as e:
            # 한 URL이 실패해도 전체가 멈추지 않도록 예외를 잡아 기록만 하고 계속 진행
            failed.append((url, str(e)))
            print(f"  -> FAILED: {e}")

        if i < len(urls):
            time.sleep(random.uniform(*delay_range))

    print(f"\ndone: {len(ok)} succeeded, {len(failed)} failed")
    for url, err in failed:
        print(f"  - {url}: {err}")
