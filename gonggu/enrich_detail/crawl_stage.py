"""1단계(분리 실행) — 크롤링+추출만. LLM은 절대 안 부른다.

runner.py의 단일실행 경로(크롤링→LLM→DB를 상품 1건마다 순서대로)와 달리, 이 모듈은
크롤링/추출까지만 하고 결과를 jsonl 체크포인트에 쌓아둔다. LLM을 기다리지 않으므로
브라우저(특히 uc — 드라이버 1개뿐)가 쉬지 않고 계속 다음 상품을 문다. 나머지(LLM→DB)는
llm_stage.py/load_stage.py가 이어서 처리한다(대공사, 2026-08-13).

크롤링 실패(gone/blocked/error)는 지금처럼 그 즉시 DB에 상태만 기록한다(write_status) —
jsonl에는 성공(크롤링 완료)한 건만 남긴다.

사용법:
    python3 -m gonggu.enrich_detail.crawl_stage                  # fast 모드 기본
    DETAIL_MODE=uc python3 -m gonggu.enrich_detail.crawl_stage   # uc 모드
    LIMIT=10 python3 -m gonggu.enrich_detail.crawl_stage
    SHARD_COUNT=5 SHARD_INDEX=0 UC_PROFILE=... python3 -m gonggu.enrich_detail.crawl_stage
        (수동으로 터미널을 직접 여러 개 열어 각자 SHARD_INDEX를 다르게 줄 때. 출력 파일도
         샤드별로 나뉜다: detail_crawled_shard{N}.jsonl)
    DETAIL_MODE=uc UC_SHARD_COUNT=3 python3 -m gonggu.enrich_detail.crawl_stage
        (2026-08-21 — 터미널 한 줄로 uc 샤딩까지 끝내는 한방 실행. uc_engine의 드라이버/락이
         프로세스 전역이라 한 프로세스 안에서 동시성을 올려도 줄서기만 할 뿐 처리량이 그대로다
         — 진짜 병렬은 이렇게 프로세스를 N개 띄우는 것뿐이다. 내부적으로 자기 자신을 SHARD_COUNT/
         SHARD_INDEX가 다른 자식 프로세스 N개로 띄우고 끝날 때까지 기다린다. 샤드 0은 기존
         UC_PROFILE(또는 DEFAULT_PROFILE)을 그대로 쓰고, 나머지는 처음 실행될 때만 그 프로필을
         복제해 자기 몫을 새로 만든다(캡차를 N번 다시 통과할 필요 없이 이미 쌓인 신뢰를 물려받음).
         눈에는 크롬 창이 N개 뜬다 — uc는 UC_HEADLESS=0이 기본이라 각 프로세스마다 실제 창을 연다.)
"""
import os
import shutil
import subprocess
import sys
import threading

from gonggu.common import DEEPSEEK_KEY, ROOT, acquire_lock, append_jsonl, connect_dst, load_jsonl
from gonggu.crawl_pool import run_crawl_pool
from gonggu.resolve_links.config import ITEM_DELAY, ITEM_DELAY_SMART, MAX_BROWSERS
from gonggu.resolve_links.urlutil import host_of

from .config import DETAIL_CONCURRENCY, DETAIL_MODE, MAX_ERROR_LEN
from .naver_uc import DEFAULT_PROFILE
from .runner import crawl_one
from .targets import fetch_captions, fetch_targets
from .writeback import write_status

OUTPUT_DIR = ROOT / 'data/output'


def _output_path(shard_count, shard_index):
    if shard_count > 1:
        return OUTPUT_DIR / f'detail_crawled_shard{shard_index}.jsonl'
    return OUTPUT_DIR / 'detail_crawled.jsonl'


def _all_crawled_keys():
    """지금까지 크롤링에 성공한 전체 key 집합(2026-08-18 수정, 문제 3) — SHARD_COUNT가
    실행마다 달라질 수 있어서(예: 어떤 날은 샤딩 없이, 어떤 날은 5-way로) 결과가 쌓이는
    파일 자체가 detail_crawled.jsonl 하나였다가 detail_crawled_shard0~4.jsonl로 바뀔 수
    있다. 자기 샤드의 출력 파일 하나만 보고 "이미 크롤링했는지"를 판단하면, 다른 샤드
    구성으로 이미 크롤링해둔 상품을 못 보고 또 크롤링하게 된다 — 특히 uc는 사람이 지켜보는
    가장 느린 자원이라 이 낭비가 제일 아프다. llm_stage.py._load_crawled()와 동일하게
    detail_crawled*.jsonl 전부를 합쳐서 봐야 한다."""
    keys = set()
    for path in sorted(OUTPUT_DIR.glob('detail_crawled*.jsonl')):
        keys.update(load_jsonl(path).keys())
    return keys


def _clone_uc_profile(base, dst):
    """base 프로필을 dst로 복제 — 새 uc 샤드가 이미 쌓인 네이버 신뢰 쿠키를 그대로 물려받게
    한다(처음부터 새 프로필로 캡차를 다시 통과해야 하는 걸 피함). Singleton*는 크롬이 실행
    중에만 쓰는 잠금 파일이라 그대로 복사하면 새 프로필이 "이미 열려있다"고 오판할 수 있어
    제외한다."""
    def _ignore(_dir, names):
        return [n for n in names if n.startswith('Singleton')]
    shutil.copytree(base, dst, ignore=_ignore)


def _run_uc_sharded(shard_count):
    """uc crawl_stage를 shard_count개의 독립 프로세스(=독립 크롬 드라이버)로 동시에 돌린다
    (2026-08-21). 자기 자신을 SHARD_COUNT/SHARD_INDEX가 다른 자식으로 N번 재실행해 끝날 때까지
    기다린다 — 파티션(product_row_id % shard_count)과 체크포인트 병합은 기존 로직을 그대로
    탄다. 반환: 0(전 샤드 성공) / 1(하나라도 실패)."""
    base = os.environ.get('UC_PROFILE') or DEFAULT_PROFILE
    profiles = [base] + [f'{base}_{i}' for i in range(1, shard_count)]
    for i, p in enumerate(profiles):
        if i == 0 or os.path.exists(p):
            continue
        if os.path.exists(base):
            print(f'  [샤드{i}] 프로필 없음 — 기존 신뢰 쿠키를 복제합니다: {base} -> {p}')
            _clone_uc_profile(base, p)
        else:
            print(f'  [샤드{i}] 기존 프로필도 없어 새로 시작합니다(캡차가 뜰 수 있음): {p}')

    log_lock = threading.Lock()
    results = [None] * shard_count

    def _run_one(i):
        env = {**os.environ, 'DETAIL_MODE': 'uc', 'SHARD_COUNT': str(shard_count),
               'SHARD_INDEX': str(i), 'UC_PROFILE': profiles[i], 'PYTHONUNBUFFERED': '1'}
        tag = f'[샤드{i}] '
        proc = subprocess.Popen([sys.executable, '-m', 'gonggu.enrich_detail.crawl_stage'],
                                 cwd=ROOT, env=env, text=True, encoding='utf-8', errors='replace',
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        for line in proc.stdout:
            with log_lock:
                sys.stdout.write(f'{tag}{line}')
                sys.stdout.flush()
        results[i] = proc.wait()

    threads = [threading.Thread(target=_run_one, args=(i,)) for i in range(shard_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    failed = [i for i, code in enumerate(results) if code != 0]
    if failed:
        print(f'uc 샤딩 {shard_count}-way — 샤드 {failed} 실패(위 로그의 [샤드N] 태그로 원인 확인)')
        return 1
    print(f'uc 샤딩 {shard_count}-way 전체 완료')
    return 0


def main():
    shard_count = int(os.environ.get('SHARD_COUNT', '1'))
    shard_index = int(os.environ.get('SHARD_INDEX', '0'))

    uc_shard_count = int(os.environ.get('UC_SHARD_COUNT', '1'))
    if DETAIL_MODE == 'uc' and uc_shard_count > 1 and 'SHARD_INDEX' not in os.environ:
        sys.exit(_run_uc_sharded(uc_shard_count))

    lock_name = f'enrich_detail_crawl_shard{shard_index}' if shard_count > 1 else 'enrich_detail_crawl'
    acquire_lock(lock_name)
    if not DEEPSEEK_KEY:
        print('.env에 DEEPSEEK_KEY가 필요합니다.', file=sys.stderr)
        sys.exit(1)

    out_path = _output_path(shard_count, shard_index)  # 이번 실행 결과는 계속 자기 샤드 파일에 씀
    already_crawled = _all_crawled_keys()  # 하지만 "이미 크롤링됐는지"는 전체를 합쳐서 판단

    only_platform = os.environ.get('PLATFORM') or None
    conn = connect_dst()
    try:
        targets = fetch_targets(conn, only_platform, DETAIL_MODE)
    finally:
        conn.close()

    skip = [h for h in os.environ.get('DETAIL_SKIP_HOSTS', '').split(',') if h]
    if os.environ.get('DETAIL_SKIP_NAVER', '0') == '1' and 'naver.' not in skip:
        skip.append('naver.')
    if skip:
        before = len(targets)
        targets = [(c, r) for c, r in targets
                   if not any(k in host_of(r['candidate_url'] or '') for k in skip)]
        print(f'  스킵 호스트 {skip} — {before - len(targets)}건 제외')

    if shard_count > 1:
        before = len(targets)
        targets = [(c, r) for c, r in targets if r['product_row_id'] % shard_count == shard_index]
        print(f'  샤딩 {shard_index}/{shard_count} — 전체 {before}건 중 이 프로세스 담당 {len(targets)}건')

    before = len(targets)
    targets = [(c, r) for c, r in targets
               if f"{c}:{r['native_id']}#{r['product_row_id']}" not in already_crawled]
    if before - len(targets):
        print(f'  이미 크롤링 완료(체크포인트에 있음) {before - len(targets)}건 제외')

    total = len(targets)
    limit = int(os.environ.get('LIMIT', '0')) or total
    targets = targets[:limit]
    print(f'[{DETAIL_MODE} 모드/crawl_stage] 크롤링 대상 {total}건 → 이번 실행 {len(targets)}건'
          f"{f' (LIMIT으로 {total - len(targets)}건 보류)' if len(targets) < total else ''}")
    if not targets:
        print('  오늘은 크롤링할 것이 없습니다.')
        return

    captions = fetch_captions(targets)
    print(f'  원본 캡션 확보 {len(captions)}건 / 대상 {len(targets)}건, → {out_path.name}')
    print(f'  — 동시 워커 상한 {DETAIL_CONCURRENCY}개, 브라우저 상한 {MAX_BROWSERS}개')

    counters = {}

    def handle(ctx, target):
        code, row = target
        db = ctx.state
        caption = captions.get((code, row['native_id']), '')
        try:
            status, facts, err, dbg = crawl_one(ctx.page, row)
        except Exception as e:  # 예상 밖 예외 — 이 상품만 error로 남기고 워커는 계속
            status, facts, err, dbg = ('error', None, f'예외: {str(e)[:MAX_ERROR_LEN - 10]}', '')

        # uc 패스에서 crawled가 아닌 결과(error/blocked)는 전부 blocked로 남긴다 — runner.py의
        # 기존 정책과 동일(uc 큐 안에 머물러 다음 uc 실행에서 다시 시도).
        if DETAIL_MODE == 'uc' and status == 'error':
            status = 'blocked'

        key = f"{code}:{row['native_id']}#{row['product_row_id']}"
        with ctx.lock:
            if status == 'crawled':
                append_jsonl(out_path, {
                    'key': key, 'code': code, 'product_row_id': row['product_row_id'],
                    'product_name': row['product_name'], 'parent_title': row.get('parent_title'),
                    'gonggu_stage': row.get('gonggu_stage'), 'publish_date': row.get('publish_date'),
                    'caption': caption, 'facts': facts,
                })
            else:
                try:
                    write_status(db, code, row['product_row_id'], status, err)
                except Exception as e:
                    status, err = 'error', f'DB 저장 실패: {str(e)[:120]}'
            counters[status] = counters.get(status, 0) + 1
            counters['_n'] = counters.get('_n', 0) + 1
            print(f"  [{counters['_n']}/{len(targets)}] (w{ctx.worker_id}) {key} -> {status} "
                  f"[{dbg}] {str(err or '')[:70]}", flush=True)

    run_crawl_pool(targets, handle, concurrency=DETAIL_CONCURRENCY, item_delay=ITEM_DELAY,
                   delay_only_after_browser=ITEM_DELAY_SMART,
                   worker_setup=connect_dst, worker_teardown=lambda db: db.close(),
                   warn_hint='DETAIL_CONCURRENCY')

    by_status = {k: v for k, v in counters.items() if not k.startswith('_')}
    print(f'크롤링 완료 {len(targets)}건 — {by_status} → {out_path}')


if __name__ == '__main__':
    main()
