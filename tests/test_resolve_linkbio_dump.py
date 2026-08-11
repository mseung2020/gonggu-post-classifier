"""resolve가 인포크 파싱본을 게시일별 JSON으로 떨구는지(2026-08-11 중복 크롤 제거) — 캐시에
이미 있는 파싱 결과만 꺼내 쓰고 재크롤하지 않는다."""
import json

from gonggu.crawl_linkbio import OUT_DIR
from gonggu.resolve_links import links, runner


def test_dump_linkbio_from_cache(monkeypatch):
    hub = 'https://link.inpock.co.kr/_pytest_dump'
    # resolve가 파싱해 캐시에 남겼다고 가정(원본 dict에 이메일이 섞여 있음)
    monkeypatch.setitem(links._linkbio_cache, hub,
                        {'data': {'platform': 'inpock', 'username': '_pytest_dump',
                                  'texts': ['📧 seller@example.com'], 'links': []}})
    item = {'platform': 'ig',
            'parent': {'post_id': '_PYTEST_POST', 'publish_date': '2026-08-06'},
            'products': [{'candidate_url': f'{hub};https://smartstore.naver.com/x/products/1'}]}
    out_file = OUT_DIR / '2026-08-06.jsonl'
    before = out_file.read_text() if out_file.exists() else None
    try:
        runner._dump_linkbio([item])
        recs = [json.loads(x) for x in out_file.read_text().splitlines() if x.strip()]
        mine = [r for r in recs if r.get('key') == 'ig:_PYTEST_POST']
        assert mine, '게시일 파일에 이 포스트 레코드가 있어야'
        rec = mine[-1]
        assert rec['publish_date'] == '2026-08-06'
        assert rec['hub_urls'] == [hub]
        assert rec['linkbio'][0]['parsed']['username'] == '_pytest_dump'
        # 이메일이 파싱본 안에 그대로 보존(별도 추출 없이)
        assert 'seller@example.com' in json.dumps(rec['linkbio'][0]['parsed'], ensure_ascii=False)
    finally:
        # 테스트가 추가한 줄 제거(원상복구)
        if before is None:
            out_file.unlink(missing_ok=True)
        else:
            out_file.write_text(before)


def test_dump_skips_when_no_cache(monkeypatch):
    # 캐시에 없는 허브(이번 실행에 파싱 안 됨)면 아무것도 안 쓴다 — 재크롤하지 않는다.
    monkeypatch.setattr(links, '_linkbio_cache', {})
    item = {'platform': 'ig', 'parent': {'post_id': '_PYTEST_NONE', 'publish_date': '2026-08-06'},
            'products': [{'candidate_url': 'https://link.inpock.co.kr/_never_parsed'}]}
    out_file = OUT_DIR / '2026-08-06.jsonl'
    before = out_file.read_text() if out_file.exists() else None
    try:
        runner._dump_linkbio([item])
        recs = [json.loads(x) for x in out_file.read_text().splitlines() if x.strip()] if out_file.exists() else []
        assert not [r for r in recs if r.get('key') == 'ig:_PYTEST_NONE']
    finally:
        if before is None:
            out_file.unlink(missing_ok=True)
        elif out_file.exists():
            out_file.write_text(before)
