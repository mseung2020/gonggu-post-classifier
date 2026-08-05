#!/usr/bin/env python3
"""데이터 하우스키핑(대공사 3단계 C2, 2026-08-05) — 무한 성장하는 파일들을 관리한다.
gonggu.daily가 마지막 단계로 자동 실행하며, 단독으로도 돌릴 수 있다.

하는 일 세 가지:

1. **append-only 체크포인트 컴팩션** — link_resolution.jsonl / period_backfill.jsonl은
   "같은 key의 마지막 줄이 이긴다" 규약의 append 전용 파일이라, rescan이 재탐색 결과를
   매일 append하면 같은 key의 옛 줄이 계속 쌓인다(실측: link_resolution 23.6MB, 매 실행마다
   전체 파싱). 규약이 명확하므로 last-wins로 접어서 다시 쓰는 건 의미 보존이 보장된다.
   COMPACT_MIN_MB(기본 20)보다 작은 파일은 건드리지 않는다(무의미한 재작성 방지).

2. **llm_usage.jsonl 로테이션** — USAGE_KEEP_DAYS(기본 30)일보다 오래된 사용량 기록을
   data/output/llm_usage_archive/<YYYY-MM>.jsonl로 옮긴다. llm_usage_report.py는 어차피
   날짜 하나만 집계하므로 최근분만 있으면 된다(옛 날짜를 보고 싶으면 아카이브 파일을 합쳐서
   보면 됨). 중간에 죽으면 재실행 시 아카이브에 중복 줄이 생길 수 있으나 사용량 로그라 무해.

3. **오래된 원본 아카이브(옵트인)** — ARCHIVE_AFTER_DAYS를 지정한 경우에만, 그 일수보다
   오래된 01_raw/01_raw_yt_ppl/02_classified 날짜 파일을 data/archive/<폴더>/<날짜>.jsonl.gz로
   압축 이동한다(JSONL은 gzip이 잘 먹혀 대략 1/5). 반드시 01을 02보다 먼저 옮긴다 — 01[d]만
   남고 02[d]가 사라지는 순간이 생기면 classify가 그 날짜 전체를 "미분류"로 착각해 재분류
   폭탄이 터진다(반대 순서/동시 소실은 무해). transform 증분 모드는 사라진 날짜를 건드리지
   않으므로 03/04는 영향 없다. 복원: `gunzip data/archive/02_classified/<날짜>.jsonl.gz` 후
   원래 폴더로 옮기면 끝.

⚠ resolve_links/rescan이 돌고 있는 동안 단독 실행하지 말 것 — 걔들이 append 중인 파일을
컴팩션이 통째로 다시 쓴다(gonggu.daily 안에서는 순차 실행이라 안전).

사용법:
    python3 -m gonggu.maintenance                       # 컴팩션 + 로테이션 (아카이브는 안 함)
    ARCHIVE_AFTER_DAYS=30 python3 -m gonggu.maintenance # + 30일 지난 원본 gzip 아카이브
    COMPACT_MIN_MB=0 python3 -m gonggu.maintenance      # 크기 무관하게 컴팩션 강제
"""
import datetime
import gzip
import json
import os
import shutil

from gonggu.common import ROOT, load_jsonl

COMPACT_TARGETS = (ROOT / 'data/output/link_resolution.jsonl',
                   ROOT / 'data/output/period_backfill.jsonl')
USAGE_FILE = ROOT / 'data/output/llm_usage.jsonl'
USAGE_ARCHIVE_DIR = ROOT / 'data/output/llm_usage_archive'
# 01을 02보다 먼저(위 docstring의 재분류 폭탄 참고).
ARCHIVE_DIRS = ('01_raw', '01_raw_yt_ppl', '02_classified')
ARCHIVE_ROOT = ROOT / 'data/archive'


def compact_jsonl(path, min_bytes):
    """last-wins(common.load_jsonl과 같은 규약)로 접어 임시파일에 쓰고 원자 교체.
    반환: (전 줄수, 후 줄수) 또는 None(파일 없음/임계 미만)."""
    if not path.exists() or path.stat().st_size < min_bytes:
        return None
    records = load_jsonl(path)  # key -> 마지막 레코드
    before = sum(1 for line in open(path, encoding='utf-8') if line.strip())
    tmp = path.with_suffix(path.suffix + '.compact.tmp')
    with open(tmp, 'w', encoding='utf-8') as f:
        for rec in records.values():
            f.write(json.dumps(rec, ensure_ascii=False) + '\n')
    os.replace(tmp, path)
    return before, len(records)


def rotate_usage(keep_days, today=None):
    """llm_usage.jsonl에서 keep_days보다 오래된 항목을 월별 아카이브로 이동.
    반환: (이동 건수, 유지 건수) 또는 None(파일 없음)."""
    if not USAGE_FILE.exists():
        return None
    today = today or datetime.date.today()
    cutoff = (today - datetime.timedelta(days=keep_days)).isoformat()
    keep, old_by_month = [], {}
    with open(USAGE_FILE, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            ts = (json.loads(line).get('ts') or '')[:10]
            if ts and ts < cutoff:
                old_by_month.setdefault(ts[:7], []).append(line)
            else:
                keep.append(line)
    if not old_by_month:
        return 0, len(keep)
    USAGE_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    for month, lines in sorted(old_by_month.items()):
        with open(USAGE_ARCHIVE_DIR / f'{month}.jsonl', 'a', encoding='utf-8') as f:
            f.write('\n'.join(lines) + '\n')
    tmp = USAGE_FILE.with_suffix('.jsonl.rotate.tmp')
    with open(tmp, 'w', encoding='utf-8') as f:
        for line in keep:
            f.write(line + '\n')
    os.replace(tmp, USAGE_FILE)
    return sum(len(v) for v in old_by_month.values()), len(keep)


def _is_date_stem(stem):
    try:
        datetime.date.fromisoformat(stem)
        return True
    except ValueError:
        return False  # '_unknown' 등은 아카이브하지 않는다


def archive_old_dates(days, today=None):
    """days보다 오래된 날짜 파일을 gzip으로 data/archive/에 이동. 반환: [(원본, 아카이브), ...]"""
    today = today or datetime.date.today()
    cutoff = (today - datetime.timedelta(days=days)).isoformat()
    moved = []
    for dirname in ARCHIVE_DIRS:
        src_dir = ROOT / 'data' / dirname
        if not src_dir.exists():
            continue
        for f in sorted(src_dir.glob('*.jsonl')):
            if not _is_date_stem(f.stem) or f.stem >= cutoff:
                continue
            dst_dir = ARCHIVE_ROOT / dirname
            dst_dir.mkdir(parents=True, exist_ok=True)
            dst = dst_dir / (f.name + '.gz')
            with open(f, 'rb') as fin, gzip.open(dst, 'wb') as fout:
                shutil.copyfileobj(fin, fout)
            f.unlink()
            moved.append((f, dst))
    return moved


def main():
    min_mb = float(os.environ.get('COMPACT_MIN_MB', '20'))
    for path in COMPACT_TARGETS:
        r = compact_jsonl(path, int(min_mb * 1024 * 1024))
        if r is None:
            size = path.stat().st_size / 1024 / 1024 if path.exists() else 0
            print(f'컴팩션 건너뜀: {path.name} ({size:.1f}MB < {min_mb}MB)')
        else:
            before, after = r
            print(f'컴팩션 완료: {path.name} — {before:,}줄 -> {after:,}줄 '
                  f'(중복 {before - after:,}줄 제거, 현재 {path.stat().st_size / 1024 / 1024:.1f}MB)')

    keep_days = int(os.environ.get('USAGE_KEEP_DAYS', '30'))
    r = rotate_usage(keep_days)
    if r:
        moved, kept = r
        if moved:
            print(f'llm_usage 로테이션: {moved:,}건을 {USAGE_ARCHIVE_DIR.name}/로 이동, 최근 {kept:,}건 유지')

    archive_days = os.environ.get('ARCHIVE_AFTER_DAYS')
    if archive_days and int(archive_days) > 0:
        moved = archive_old_dates(int(archive_days))
        if moved:
            print(f'아카이브: {len(moved)}개 파일을 data/archive/로 gzip 이동 —')
            for src, dst in moved:
                print(f'  {src.parent.name}/{src.name} -> {dst.relative_to(ROOT)}')
        else:
            print(f'아카이브: {archive_days}일 지난 날짜 파일 없음')
    else:
        print('아카이브: 비활성 (원하면 ARCHIVE_AFTER_DAYS=30 처럼 지정 — 그 일수 지난 '
              '01_raw/01_raw_yt_ppl/02_classified 날짜 파일을 gzip으로 data/archive/에 이동)')


if __name__ == '__main__':
    main()
