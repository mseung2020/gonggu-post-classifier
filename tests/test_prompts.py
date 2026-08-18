"""prompts.py — LLM#1의 url_type 판별 예시가 실제 linkbio_parser 지원 목록과 어긋나지
않는지(2026-08-18 점검, 문제 11). 예전엔 "링크모음" 예시 도메인이 하드코딩돼 있어서
lit.link·taplink처럼 실제로는 지원 안 하는 도메인이 남아있고 hity.io/bio.site 등 실제
지원하는 도메인은 빠져있었다 — 지금은 linkbio_parser.hosts.SUPPORTED_HOSTS에서 동적으로
가져오므로 이 값이 바뀌면 프롬프트도 같이 맞춰진다."""
from gonggu.linkbio_parser.hosts import SUPPORTED_HOSTS
from gonggu.prompts import GONGGU_CLASSIFY_SYSTEM, YT_PPL_GONGGU_SYSTEM


class TestLinkbioHostsInSyncWithPrompts:
    def test_no_leftover_placeholder(self):
        assert '{LINKBIO_HOSTS}' not in GONGGU_CLASSIFY_SYSTEM
        assert '{LINKBIO_HOSTS}' not in YT_PPL_GONGGU_SYSTEM

    def test_every_supported_host_appears_in_both_prompts(self):
        for host in SUPPORTED_HOSTS:
            assert host in GONGGU_CLASSIFY_SYSTEM, f'{host}가 LLM#1 프롬프트에 없음'
            assert host in YT_PPL_GONGGU_SYSTEM, f'{host}가 YT PPL 프롬프트에 없음'

    def test_unsupported_legacy_examples_are_gone(self):
        """예전에 하드코딩돼 있었지만 실제로는 지원 안 하는 도메인(lit.link/taplink)이
        더 이상 예시로 남아있지 않아야 한다 — 실제 지원 목록에 없으면서 프롬프트에만
        남아있으면 LLM이 잘못된 신호를 학습하게 된다."""
        for stale in ('lit.link', 'taplink'):
            assert stale not in GONGGU_CLASSIFY_SYSTEM
            assert stale not in YT_PPL_GONGGU_SYSTEM
