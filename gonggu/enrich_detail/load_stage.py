"""3단계(분리 실행) — DB 반영만. LLM/크롤링과 완전히 무관하다.

llm_stage.py가 쌓아둔 detail_llm.jsonl을 읽어 write_done(UPSERT)으로 DB에 반영한다.
write_done 자체가 이미 상품 1건 단위 트랜잭션(detail UPSERT + 이미지 전체교체, writeback.py)
이라 별도 배치 커밋을 얹지 않는다 — load.py의 소배치 커밋은 "새 행을 대량 INSERT"할 때
DB 왕복을 줄이려는 것이고, 여기는 이미 있는 행을 UPSERT하는 것이라 단위가 다르다.

이미 DB에 반영한 key는 DETAIL_LOADED_KEYS_FILE에 남겨 다음 실행에서 건너뛴다(2026-08-18
수정, 문제 1) — detail_llm.jsonl은 압축 대상도 아니고(maintenance.py의 COMPACT_TARGETS에
없음) append-only로 영구 보존되는데, 예전엔 "UPSERT라 재실행해도 안전(멱등)하다"는 이유로
체크포인트 없이 매번 그 전체를 DB에 다시 썼다 — 정확성은 보장되지만 이 단계를 반복 실행할
때마다 이미 반영된 레코드까지 매번 다시 쓰는 비용이 무기한 쌓인다(누적 레코드 수만큼 매번
UPSERT 트랜잭션을 반복). classify.py가 겪었던 것과 같은 종류의 문제를 애초에 만들지 않기
위해, 같은 인덱스 패턴(common.load_classify_done_keys 참고)을 여기도 적용한다.

사용법:
    python3 -m gonggu.enrich_detail.load_stage
    LIMIT=50 python3 -m gonggu.enrich_detail.load_stage
"""
import os

from gonggu.common import ROOT, acquire_lock, append_jsonl, connect_dst, load_jsonl

from .writeback import write_done, write_status

OUTPUT_DIR = ROOT / 'data/output'
LLM_OUT_PATH = OUTPUT_DIR / 'detail_llm.jsonl'
LOADED_KEYS_PATH = OUTPUT_DIR / 'detail_loaded_keys.jsonl'


def _todo_records(all_records, loaded_keys):
    """이미 DB에 반영된 key(loaded_keys)는 제외하고 나머지 레코드만 남긴다."""
    return [all_records[k] for k in all_records if k not in loaded_keys]


def main():
    acquire_lock('enrich_detail_load')
    all_records = load_jsonl(LLM_OUT_PATH)  # {key: record}
    loaded_keys = set(load_jsonl(LOADED_KEYS_PATH).keys())
    records = _todo_records(all_records, loaded_keys)

    limit = int(os.environ.get('LIMIT', '0')) or len(records)
    records = records[:limit]
    print(f'LLM 처리 완료분 {len(all_records)}건 | 이미 DB 반영됨 {len(loaded_keys)}건 | '
          f'이번 실행 {len(records)}건 → DB 반영 시작'
          f"{f' (LIMIT={limit})' if limit != len(records) else ''}")
    if not records:
        print('  반영할 것이 없습니다.')
        return

    db = connect_dst()
    ok = err = 0
    try:
        for i, r in enumerate(records, 1):
            try:
                write_done(db, r['code'], r['product_row_id'], r['fields'], r['image_rows'])
                ok += 1
                append_jsonl(LOADED_KEYS_PATH, {'key': r['key']})
            except Exception as e:
                err += 1
                try:
                    write_status(db, r['code'], r['product_row_id'], 'error',
                                 f'DB 반영 실패: {str(e)[:120]}')
                except Exception:
                    pass
            if i % 100 == 0 or i == len(records):
                print(f'  {i}/{len(records)} 반영 — 성공 {ok} / 실패 {err}')
    finally:
        db.close()

    print(f'DB 반영 완료 — 성공 {ok} / 실패 {err}')


if __name__ == '__main__':
    main()
