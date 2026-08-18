"""2단계(분리 실행) — LLM#4+#5만. 브라우저/uc와 완전히 무관하다.

crawl_stage.py가 쌓아둔 jsonl 체크포인트(detail_crawled*.jsonl — 샤딩 실행이면 여러 파일)를
읽어 LLM#5(상세)+LLM#4(카테고리)를 부르고, 결과를 detail_llm.jsonl에 쌓는다. 크롤링과 완전히
분리됐으므로 llm_batch.run_llm_batch(classify_category.py 등이 쓰는 것과 동일한 배관)로
50~100개씩 병렬 호출한다 — 상품 1건 안에서 두 LLM을 스레드풀로 또 쪼개던 process_target의
방식(runner.py)은 여기선 불필요(바깥 배치 동시성이 이미 충분히 높음).

LLM 실패는 그 즉시 DB에 상태만 기록한다(write_status, error) — jsonl에는 성공한 건만 남긴다.

크롤링 결과(detail_crawled*.jsonl)는 LLM 결과와 별도 파일이라 영구 보존된다 — 나중에 가격/
배송비 판정용 프롬프트를 고쳐서 이미 처리된 것까지 처음부터 다시 돌리고 싶을 때, 크롤링을
또 할 필요 없이 이 저장된 facts/caption을 그대로 재사용하면 된다(FORCE_RELLM=1).

사용법:
    python3 -m gonggu.enrich_detail.llm_stage
    LLM_STAGE_CONCURRENCY=100 python3 -m gonggu.enrich_detail.llm_stage
    LIMIT=50 python3 -m gonggu.enrich_detail.llm_stage
    FORCE_RELLM=1 python3 -m gonggu.enrich_detail.llm_stage   # 프롬프트 수정 후 전부 재처리
"""
import os
import sys

from gonggu.common import DEEPSEEK_KEY, ROOT, acquire_lock, append_jsonl, connect_dst, load_jsonl
from gonggu.llm_batch import run_llm_batch

from .images import build_image_rows
from .llm import call_category, call_detail_enrich
from .validate import merge_and_validate
from .writeback import write_status

OUTPUT_DIR = ROOT / 'data/output'
LLM_OUT_PATH = OUTPUT_DIR / 'detail_llm.jsonl'


def _load_crawled():
    """detail_crawled.jsonl + detail_crawled_shard*.jsonl 전부를 합쳐 {key: record}로."""
    merged = {}
    for path in sorted(OUTPUT_DIR.glob('detail_crawled*.jsonl')):
        merged.update(load_jsonl(path))
    return merged


def process_one(item):
    """크롤링 결과 1건 → LLM#5+#4 → 검증. 반환: item 원본 + (fields/image_rows) 또는 llm_error."""
    facts = item['facts']
    caption = item.get('caption') or ''
    llm_out, llm_err = call_detail_enrich(
        product_name=item['product_name'], caption=caption, facts=facts,
        gonggu_stage=item.get('gonggu_stage'), publish_date=item.get('publish_date'))
    category, subcategory = call_category(
        product_name=facts.get('product_name') or item['product_name'],
        title=item.get('parent_title') or '', caption=caption)
    if llm_out is None:
        return {**item, 'llm_error': f'LLM#5 실패: {llm_err}'}
    fields = merge_and_validate(llm_out, facts, caption, category, subcategory)
    image_rows = build_image_rows(facts['thumbnail_urls'], facts['detail_image_urls'])
    return {'key': item['key'], 'code': item['code'], 'product_row_id': item['product_row_id'],
            'fields': fields, 'image_rows': image_rows, 'llm_error': None}


def main():
    acquire_lock('enrich_detail_llm')
    if not DEEPSEEK_KEY:
        print('.env에 DEEPSEEK_KEY가 필요합니다.', file=sys.stderr)
        sys.exit(1)

    crawled = _load_crawled()
    # FORCE_RELLM=1 — 프롬프트를 고쳐서 이미 처리된 것까지 전부 다시 LLM에 태우고 싶을 때.
    # 크롤링 결과(detail_crawled*.jsonl)는 그대로 재사용하고 LLM만 다시 부른다(재크롤링 없음).
    done_keys = set() if os.environ.get('FORCE_RELLM') == '1' else set(load_jsonl(LLM_OUT_PATH).keys())
    todo = [r for k, r in crawled.items() if k not in done_keys]

    limit = int(os.environ.get('LIMIT', '0')) or len(todo)
    todo = todo[:limit]
    concurrency = int(os.environ.get('LLM_STAGE_CONCURRENCY', '60'))
    print(f'크롤링 완료분 {len(crawled)}건 | 이미 LLM 처리됨 {len(done_keys)}건 | '
          f'이번 실행 {len(todo)}건 (동시 {concurrency})')
    if not todo:
        print('  LLM 처리할 것이 없습니다.')
        return

    # persist_one은 run_llm_batch 안에서 lock으로 감싸 호출되므로(항상 한 번에 하나) DB
    # 커넥션 하나를 여기서 공유해도 스레드 경합이 없다 — 워커별 커넥션 풀이 필요 없다.
    db = connect_dst()

    def persist_one(r):
        if r.get('llm_error'):
            try:
                write_status(db, r['code'], r['product_row_id'], 'error', r['llm_error'])
            except Exception as e:
                print(f"  ⚠ DB 상태 저장 실패({r['key']}): {str(e)[:120]}")
            return
        append_jsonl(LLM_OUT_PATH, r)

    try:
        counters = run_llm_batch(todo, process_one, persist_one, concurrency=concurrency,
                                 error_of=lambda r: r.get('llm_error'))
    finally:
        db.close()

    print(f"LLM 처리 완료 — 성공 {counters['ok']} / 실패 {counters['err']} → {LLM_OUT_PATH}")


if __name__ == '__main__':
    main()
