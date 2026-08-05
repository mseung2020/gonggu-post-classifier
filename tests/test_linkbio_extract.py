"""linkbio_parser/extract.py — HTML에 embed된 JSON 추출기의 경계 사례."""
import base64
import json

import pytest

from gonggu.linkbio_parser.extract import extract_balanced_json, extract_raw
from gonggu.linkbio_parser.hosts import detect_platform


class TestBalancedJson:
    def test_nested_braces(self):
        html = 'var x=1; window.__data = {"a": {"b": [1, 2, {"c": 3}]}}; other()'
        assert json.loads(extract_balanced_json(html, 'window.__data')) == {'a': {'b': [1, 2, {'c': 3}]}}

    def test_braces_inside_strings_ignored(self):
        html = 'window.__data = {"t": "중괄호 } 포함 \\" 문자열"}; f()'
        assert json.loads(extract_balanced_json(html, 'window.__data'))['t'] == '중괄호 } 포함 " 문자열'

    def test_marker_missing(self):
        assert extract_balanced_json('nothing here', 'window.__data') is None

    def test_array_root(self):
        html = 'window.initial_state = [{"x": 1}] ;'
        assert json.loads(extract_balanced_json(html, 'window.initial_state')) == [{'x': 1}]


class TestExtractRaw:
    def test_littly_base64(self):
        payload = base64.b64encode(json.dumps({'links': [1]}).encode()).decode()
        html = f'<script id="data" type="text/plain">{payload}</script>'
        assert extract_raw('littly', html) == {'links': [1]}

    def test_next_data_platforms(self):
        html = ('<script id="__NEXT_DATA__" type="application/json">'
                '{"props": {"pageProps": {"profile": {"name": "x"}}}}</script>')
        assert extract_raw('inpock', html) == {'profile': {'name': 'x'}}

    def test_linkon_hidden_input(self):
        html = ('<input id="jsonLinkList" value="[{&quot;title&quot;: &quot;구매&quot;}]">'
                '<meta property="og:title" content="샵이름">')
        out = extract_raw('linkon', html)
        assert out['linkList'] == [{'title': '구매'}] and out['title'] == '샵이름'

    def test_structure_change_raises(self):
        with pytest.raises(ValueError):
            extract_raw('littly', '<html>구조가 바뀐 페이지</html>')


class TestDetectPlatform:
    def test_known_hosts(self):
        assert detect_platform('https://litt.ly/x') == 'littly'
        assert detect_platform('https://inpk.link/x') == 'inpock'
        assert detect_platform('https://tr.ee/abc') == 'linktree'

    def test_unknown_host_raises(self):
        with pytest.raises(ValueError):
            detect_platform('https://smartstore.naver.com/x')
