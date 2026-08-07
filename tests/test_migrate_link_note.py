"""link_note 소급 백필의 key 파싱 검증 — jsonl key(platform:native_id:sort_order)를
DB 매칭용 (code, native_id, sort_order)로 정확히 되돌리는지 못박는다."""
from gonggu._migrate_link_note import parse_key


def test_basic_ig():
    assert parse_key('ig:POST123:0') == ('ig', 'POST123', 0)


def test_basic_yt():
    assert parse_key('yt:VIDEO_9:2') == ('yt', 'VIDEO_9', 2)


def test_native_id_with_colon_preserved():
    # native_id에 혹시 ':'가 들어와도 가운데는 통째로 보존(앞=platform, 뒤=sort_order만 뗌).
    assert parse_key('ig:a:b:c:3') == ('ig', 'a:b:c', 3)


def test_unknown_platform_rejected():
    assert parse_key('tiktok:X:0') is None


def test_non_integer_sort_order_rejected():
    assert parse_key('ig:POST:notanint') is None


def test_too_few_parts_rejected():
    assert parse_key('ig:POST') is None
    assert parse_key('garbage') is None
