"""linkbio_parser/emails.py — 파싱 결과에서 이메일만 뽑는 로직(플랫폼 무관)."""
from gonggu.linkbio_parser import extract_emails


def test_finds_email_in_bio_text():
    parsed = {'platform': 'inpock', 'bio': '문의는 seller@example.com 으로 주세요', 'links': []}
    assert extract_emails(parsed) == ['seller@example.com']


def test_finds_email_in_sns_value_and_link_title():
    parsed = {'sns': [{'type': 'instagram', 'value': 'not-an-email'},
                       {'type': 'etc', 'value': 'contact@brand.co.kr'}],
              'links': [{'title': '문의 dm@shop.com', 'url': 'https://shop.com/dm@shop.com'}]}
    found = extract_emails(parsed)
    assert 'contact@brand.co.kr' in found
    assert 'dm@shop.com' in found
    # url 필드 자체는 스캔 대상이 아니므로 같은 이메일이 두 번 잡히더라도 중복 제거된 채 1개만 남는다
    assert found.count('dm@shop.com') == 1


def test_skips_link_fields():
    parsed = {'url': 'https://a@b.example/x', 'resolved_url': 'https://c@d.example/y',
              'image': 'https://e@f.example/z.png', 'bio': ''}
    assert extract_emails(parsed) == []


def test_no_email_returns_empty_list():
    assert extract_emails({'bio': '이메일 없음', 'links': []}) == []


def test_preserves_first_seen_order():
    parsed = {'bio': 'a@x.com', 'notice': 'b@x.com', 'title': 'a@x.com'}
    assert extract_emails(parsed) == ['a@x.com', 'b@x.com']
