"""resolve가 링크인바이오 파싱본을 게시일별 JSON으로 떨구는지(2026-08-11 중복 크롤 제거,
인포크 한정 해제) — 캐시에 이미 있는 파싱 결과만 꺼내 쓰고 재크롤하지 않는다. 곁다리 이메일
수집(HIFEN_EMAIL_FILE)도 여기서 같이 검증한다."""
import json

from gonggu.common import HIFEN_EMAIL_FILE
from gonggu.crawl_linkbio import OUT_DIR
from gonggu.resolve_links import links, runner


def _read_records(path):
    """JSONL을 **운영 코드와 같은 방식**으로 읽는다(common.load_jsonl은 `for line in f`).

    ⚠ str.splitlines()를 쓰면 안 된다 — 그건 '\\n' 말고도 U+2028(LINE SEPARATOR), U+2029,
    \\x0b, \\x0c, \\x85까지 줄바꿈으로 보는데 JSON은 아니다. 실제로 2026-08-20에 어떤
    크리에이터 bio에 U+2028이 하나 들어오면서 레코드 하나가 반토막 나 이 테스트 3개가
    한꺼번에 깨졌다(파일 자체는 멀쩡했고, 운영 경로도 멀쩡했다 — 테스트만 다르게 읽고 있었다)."""
    if not path.exists():
        return []
    with open(path, encoding='utf-8') as f:
        return [json.loads(line) for line in f if line.strip()]


def test_dump_linkbio_from_cache(monkeypatch):
    hub = 'https://link.inpock.co.kr/_pytest_dump'
    # resolve가 파싱해 영구 저장소에 남겼다고 가정(원본 dict에 이메일이 섞여 있음). 프로세스
    # 로컬 캐시(links._linkbio_cache)가 아니라 load_persisted_linkbio_data를 통해 읽으므로
    # (문제 8 수정), 이 함수가 돌려주는 값을 몽키패치한다 — 실제 파싱이 다른 프로세스(샤드)에서
    # 일어났어도 _dump_linkbio가 정상 동작함을 검증하는 것과 같은 경계.
    monkeypatch.setattr(runner, 'load_persisted_linkbio_data', lambda: {
        hub: {'platform': 'inpock', 'username': '_pytest_dump',
              'texts': ['📧 seller@example.com'], 'links': []}})
    item = {'platform': 'ig',
            'parent': {'post_id': '_PYTEST_POST', 'publish_date': '2026-08-06'},
            'products': [{'candidate_url': f'{hub};https://smartstore.naver.com/x/products/1'}]}
    out_file = OUT_DIR / '2026-08-06.jsonl'
    before = out_file.read_text() if out_file.exists() else None
    try:
        runner._dump_linkbio([item])
        recs = _read_records(out_file)
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


def test_dump_linkbio_non_inpock_platform_and_email_sync_file(monkeypatch):
    # 인포크가 아닌 링크인바이오 플랫폼(링크트리)도 파싱본/이메일 추출 대상이어야 한다.
    hub = 'https://linktr.ee/_pytest_dump2'
    monkeypatch.setattr(runner, 'load_persisted_linkbio_data', lambda: {
        hub: {'platform': 'linktree', 'username': '_pytest_dump2',
              'bio': '문의 contact@brand.com', 'links': []}})
    item = {'platform': 'ig',
            'parent': {'post_id': '_PYTEST_POST2', 'publish_date': '2026-08-06',
                       'user_id': '_pytest_user2'},
            'products': [{'candidate_url': hub}]}
    out_file = OUT_DIR / '2026-08-06.jsonl'
    before_out = out_file.read_text() if out_file.exists() else None
    before_email = HIFEN_EMAIL_FILE.read_text() if HIFEN_EMAIL_FILE.exists() else None
    try:
        runner._dump_linkbio([item])
        recs = _read_records(out_file)
        mine = [r for r in recs if r.get('key') == 'ig:_PYTEST_POST2']
        assert mine, '링크트리 허브를 가진 포스트도 게시일 파일에 남아야'
        rec = mine[-1]
        assert rec['hub_urls'] == [hub]
        assert rec['emails'] == 'contact@brand.com'

        email_recs = _read_records(HIFEN_EMAIL_FILE)
        mine_email = [r for r in email_recs if r.get('key') == '_pytest_user2']
        assert mine_email, '이메일이 발견된 ig 계정은 HIFEN_EMAIL_FILE에도 남아야'
        assert mine_email[-1]['emails'] == ['contact@brand.com']
    finally:
        if before_out is None:
            out_file.unlink(missing_ok=True)
        else:
            out_file.write_text(before_out)
        if before_email is None:
            HIFEN_EMAIL_FILE.unlink(missing_ok=True)
        else:
            HIFEN_EMAIL_FILE.write_text(before_email)


def test_linkbio_candidates_persists_across_process_boundary(monkeypatch):
    """문제 8 회귀 테스트(2026-08-18) — 샤딩된 실행에서는 실제 파싱이 일어난 프로세스와
    _dump_linkbio를 부르는 --finalize 프로세스가 다르다. links.linkbio_candidates()가 파싱에
    성공하면 프로세스 메모리(_linkbio_cache)뿐 아니라 디스크(LINKBIO_HUB_CACHE_FILE)에도 남아,
    메모리 캐시를 완전히 비운(다른 프로세스를 흉내 낸) 뒤에도 load_persisted_linkbio_data가
    그 결과를 여전히 볼 수 있어야 한다."""
    hub = 'https://link.inpock.co.kr/_pytest_persist'
    fake_data = {'platform': 'inpock', 'username': '_pytest_persist', 'links': []}
    monkeypatch.setattr(links, '_fetch_linkbio_candidates', lambda url: ([], fake_data))

    cache_file = links.LINKBIO_HUB_CACHE_FILE
    before = cache_file.read_text() if cache_file.exists() else None
    try:
        links.linkbio_candidates(hub)
        links._linkbio_cache.clear()  # "다른 프로세스"를 흉내 — 메모리 캐시를 완전히 비운다
        assert links.load_persisted_linkbio_data().get(hub) == fake_data
    finally:
        if before is None:
            cache_file.unlink(missing_ok=True)
        else:
            cache_file.write_text(before)


def test_dump_skips_when_no_cache(monkeypatch):
    # 영구 저장소에도 없는 허브(어느 프로세스도 파싱 안 함)면 아무것도 안 쓴다 — 재크롤하지 않는다.
    monkeypatch.setattr(runner, 'load_persisted_linkbio_data', lambda: {})
    item = {'platform': 'ig', 'parent': {'post_id': '_PYTEST_NONE', 'publish_date': '2026-08-06'},
            'products': [{'candidate_url': 'https://link.inpock.co.kr/_never_parsed'}]}
    out_file = OUT_DIR / '2026-08-06.jsonl'
    before = out_file.read_text() if out_file.exists() else None
    try:
        runner._dump_linkbio([item])
        recs = _read_records(out_file)
        assert not [r for r in recs if r.get('key') == 'ig:_PYTEST_NONE']
    finally:
        if before is None:
            out_file.unlink(missing_ok=True)
        elif out_file.exists():
            out_file.write_text(before)
