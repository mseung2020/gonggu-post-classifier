"""속도 개선 공사 A단계(2026-08-18): '패스트패스 적중률 합계'만으론 왜 낮은지, 실제 브라우저까지
간 비율이 얼마인지 알 수 없어서 B(필터 보정)/C(워커:브라우저 비율 재튜닝)를 감으로 할 수밖에
없었다. bump_via/via_stats(browser.py)로 후보 URL이 어느 경로로 처리됐는지 세고,
core.py의 두 분기(링크인바이오 구조화/uc 호스트 스킵)가 그걸 호출하는지, runner.py가 그 분포를
사람이 읽을 수 있게 찍는지를 검증한다. 실제 크롬/네트워크 없이 카운터와 출력 포맷만 본다."""
import pytest

from gonggu.resolve_links import browser, core, httpfetch
from gonggu.resolve_links.runner import (_bucket_body_lengths, _print_resolution_diagnostics,
                                         _should_print_interim_diag)


@pytest.fixture(autouse=True)
def _reset_via_stats(monkeypatch):
    """모듈 전역 카운터라 테스트끼리 격리하려면 매번 새 dict/리스트로 갈아끼운다."""
    monkeypatch.setattr(browser, '_via_stats', {})
    monkeypatch.setattr(httpfetch, '_body_len_samples', [])
    monkeypatch.setattr(httpfetch, '_stats', {'tried': 0, 'hit': 0})


class TestBumpVia:
    def test_counts_accumulate_per_key(self):
        browser.bump_via('http')
        browser.bump_via('http')
        browser.bump_via('browser')
        assert browser.via_stats() == {'http': 2, 'browser': 1}

    def test_starts_empty(self):
        assert browser.via_stats() == {}


class TestCoreBumpsCorrectVia:
    def test_linkbio_structured_bumped_before_finalize(self, monkeypatch):
        monkeypatch.setattr(core, 'linkbio_candidates', lambda url: [{'href': 'https://x', 'source': 'link'}])
        monkeypatch.setattr(core, 'finalize_pick', lambda *a, **k: {'status': 'done', 'final_url': 'https://x'})
        core._resolve_one_candidate(None, 'https://link.inpock.co.kr/abc', {'product_name': 'p'}, '')
        assert browser.via_stats() == {'linkbio_structured': 1}

    def test_uc_host_skip_bumped(self, monkeypatch):
        monkeypatch.delenv('RESOLVE_UC', raising=False)
        monkeypatch.setattr(core, 'linkbio_candidates', lambda url: None)
        res = core._resolve_one_candidate(None, 'https://smartstore.naver.com/x/products/1',
                                          {'product_name': 'p'}, '')
        assert browser.via_stats() == {'uc_host_skip': 1}
        assert res['status'] == 'unresolved'


class TestPrintResolutionDiagnostics:
    def test_prints_hit_rate_and_top_miss_reasons(self, capsys):
        hs = {'tried': 10, 'hit': 4, 'miss:no_title': 3, 'miss:body_too_short': 2,
              'miss:antibot_text': 1}
        _print_resolution_diagnostics(hs, {})
        out = capsys.readouterr().out
        assert '4/10건 적중 (40.0%)' in out
        # 많은 순으로 정렬돼야 한다
        assert out.index('no_title 3건') < out.index('body_too_short 2건') < out.index('antibot_text 1건')

    def test_prints_via_breakdown_with_percentages(self, capsys):
        via = {'linkbio_structured': 5, 'uc_host_skip': 2, 'http': 2, 'browser': 1}
        _print_resolution_diagnostics({'tried': 0, 'hit': 0}, via)
        out = capsys.readouterr().out
        assert '후보 URL 페치 시도 10건' in out
        assert 'linkbio_structured 5건(50%)' in out
        assert 'browser 1건(10%)' in out

    def test_nothing_printed_when_no_data(self, capsys):
        _print_resolution_diagnostics({'tried': 0, 'hit': 0}, {})
        assert capsys.readouterr().out == ''


class TestBodyTooShortSamples:
    """B단계(2026-08-18) — 실측 결과 패스트패스 미스의 75~80%가 body_too_short 하나였다.
    호스트+실제 길이를 최근 표본만 들고 있다가 근거로 쓴다."""

    def test_record_appends_host_and_length(self):
        httpfetch._record_body_too_short('https://shop.example.com/a', 42)
        httpfetch._record_body_too_short('https://store.kakao.com/b', 0)
        assert httpfetch.body_too_short_samples() == [
            ('shop.example.com', 42), ('store.kakao.com', 0)]

    def test_record_also_bumps_miss_counter(self):
        httpfetch._record_body_too_short('https://x.example/a', 10)
        assert httpfetch.stats()['miss:body_too_short'] == 1

    def test_samples_capped_to_recent_max(self):
        for i in range(httpfetch._BODY_LEN_SAMPLES_MAX + 10):
            httpfetch._record_body_too_short(f'https://x{i}.example/a', i)
        samples = httpfetch.body_too_short_samples()
        assert len(samples) == httpfetch._BODY_LEN_SAMPLES_MAX
        assert samples[-1] == (f'x{httpfetch._BODY_LEN_SAMPLES_MAX + 9}.example', httpfetch._BODY_LEN_SAMPLES_MAX + 9)


class TestBucketBodyLengths:
    def test_splits_into_three_bands_by_threshold_fraction(self):
        # 문턱 200 기준: <40 = near_zero, 40~160 = mid, >=160 = near_threshold
        samples = [('a', 0), ('b', 39), ('c', 40), ('d', 100), ('e', 159), ('f', 160), ('g', 199)]
        assert _bucket_body_lengths(samples, 200) == {'near_zero': 2, 'mid': 3, 'near_threshold': 2}

    def test_empty_samples(self):
        assert _bucket_body_lengths([], 200) == {'near_zero': 0, 'mid': 0, 'near_threshold': 0}


class TestPrintIncludesBodyLengthBuckets:
    def test_prints_bucket_breakdown_when_samples_present(self, capsys):
        samples = [('a.example', 0), ('b.example', 190)]
        _print_resolution_diagnostics({'tried': 0, 'hit': 0}, {}, samples)
        out = capsys.readouterr().out
        assert 'body_too_short 길이 분포' in out
        assert '표본 2건' in out

    def test_no_bucket_line_when_no_samples(self, capsys):
        _print_resolution_diagnostics({'tried': 0, 'hit': 0}, {}, [])
        assert 'body_too_short 길이 분포' not in capsys.readouterr().out


class TestShouldPrintInterimDiag:
    """오래 걸리는 배치(2026-08-18 실측, 5,084건 남음)도 다 끝나기 전에 중간 진단을 볼 수
    있어야 한다는 요청으로 추가 — '이번 실행에서 처리한 건수' 기준으로 N건마다 찍는다."""

    def test_fires_on_exact_multiples(self):
        assert _should_print_interim_diag(500, 500) is True
        assert _should_print_interim_diag(1000, 500) is True

    def test_does_not_fire_between_multiples(self):
        assert _should_print_interim_diag(499, 500) is False
        assert _should_print_interim_diag(501, 500) is False

    def test_interval_zero_disables(self):
        assert _should_print_interim_diag(500, 0) is False
        assert _should_print_interim_diag(0, 0) is False
