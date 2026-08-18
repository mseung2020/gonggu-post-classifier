"""3단계(분리 실행) — DB 반영만. LLM/크롤링과 완전히 무관하다.

llm_stage.py가 쌓아둔 detail_llm.jsonl을 읽어 write_done(UPSERT)으로 DB에 반영한다.
write_done 자체가 이미 상품 1건 단위 트랜잭션(detail UPSERT + 이미지 전체교체, writeback.py)
이라 별도 배치 커밋을 얹지 않는다 — load.py의 소배치 커밋은 "새 행을 대량 INSERT"할 때
DB 왕복을 줄이려는 것이고, 여기는 이미 있는 행을 UPSERT하는 것이라 단위가 다르다.
UPSERT라 재실행해도 안전(멱등)하므로 "이미 done인 건 스킵" 같은 별도 로직도 없다.

사용법:
    python3 -m gonggu.enrich_detail.load_stage
    LIMIT=50 python3 -m gonggu.enrich_detail.load_stage
"""
import os

from gonggu.common import ROOT, acquire_lock, connect_dst, load_jsonl

from .writeback import write_done, write_status

OUTPUT_DIR = ROOT / 'data/output'
LLM_OUT_PATH = OUTPUT_DIR / 'detail_llm.jsonl'


def main():
    acquire_lock('enrich_detail_load')
    records = list(load_jsonl(LLM_OUT_PATH).values())
    limit = int(os.environ.get('LIMIT', '0')) or len(records)
    records = records[:limit]
    print(f'LLM 처리 완료분 {len(records)}건 → DB 반영 시작'
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
