"""transform.py 순수 함수 단위 테스트 — 게이트 규칙의 경계 사례들을 박제한다."""
import pytest

from gonggu.transform import (_compute_stage, _norm_dt_str, _now_iso, _product_row,
                              _valid_date, _valid_dt, transform_one)


@pytest.fixture(autouse=True)
def _freeze_today(monkeypatch):
    monkeypatch.setenv('GONGGU_TODAY', '2026-08-05')


class TestValidDate:
    def test_normal(self):
        assert _valid_date('2026-08-01') == '2026-08-01'

    def test_datetime_truncated_to_date(self):
        assert _valid_date('2026-08-01 12:30:00') == '2026-08-01'

    def test_llm_garbage_is_none(self):
        assert _valid_date(None) is None
        assert _valid_date('') is None
        assert _valid_date('미정') is None
        assert _valid_date('2026-13-01') is None  # 13월
        assert _valid_date('2026-00-10') is None


class TestComputeStage:
    def test_before_start(self):
        assert _compute_stage('2026-08-10', '2026-08-20') == '시작전'

    def test_in_progress(self):
        assert _compute_stage('2026-08-01', '2026-08-10') == '진행중'

    def test_ended(self):
        assert _compute_stage('2026-07-01', '2026-07-10') == '종료'

    def test_start_only_today_or_past_is_in_progress(self):
        assert _compute_stage('2026-08-05', None) == '진행중'
        assert _compute_stage('2026-08-01', None) == '진행중'

    def test_end_only(self):
        assert _compute_stage(None, '2026-08-04') == '종료'
        assert _compute_stage(None, '2026-08-05') == '진행중'  # 마감일 당일은 아직 진행중

    def test_no_dates(self):
        assert _compute_stage(None, None) == '판단불가'


class TestProductRow:
    def test_urls_joined_with_semicolon_and_truncated(self):
        p = {'name': ' 상품 ', 'link_location': '설명_직접링크', 'url_type': '네이버_스마트스토어',
             'urls': ['https://a.example', '', 'https://b.example']}
        row = _product_row(p, 3)
        assert row['product_name'] == '상품'
        assert row['candidate_url'] == 'https://a.example;https://b.example'
        assert row['sort_order'] == 3

    def test_invalid_link_location_falls_back(self):
        row = _product_row({'name': 'x', 'link_location': 'LLM이지어낸값', 'urls': []}, 0)
        assert row['link_location'] == '링크없음_불명'
        assert row['candidate_url'] is None

    def test_url_type_none_normalized(self):
        assert _product_row({'name': 'x', 'url_type': '없음', 'urls': []}, 0)['url_type'] is None

    def test_candidate_url_length_cap_500(self):
        p = {'name': 'x', 'urls': ['https://e.example/' + 'a' * 600]}
        assert len(_product_row(p, 0)['candidate_url']) == 500


class TestTransformOneGates:
    def _post(self, **cls):
        return {'platform': 'ig', 'post_id': 'P1', 'user_id': 'u1', 'url': 'https://insta/p/P1',
                'publish_date': '2026-08-01 10:00:00',
                'description': '공구 오픈합니다', 'classification': cls or None}

    def test_classification_error_rejected(self):
        post = self._post()
        post['classification'] = None
        post['classification_error'] = 'Read timed out'
        _, _, reject = transform_one(post)
        assert reject and '분류실패' in reject

    def test_not_gonggu_rejected(self):
        _, _, reject = transform_one(self._post(is_gonggu=False))
        assert reject == 'is_gonggu=false'

    def test_gonggu_without_products_rejected(self):
        _, _, reject = transform_one(self._post(is_gonggu=True, products=[]))
        assert reject and 'products' in reject

    def test_affiliate_topn_rejected(self):
        post = self._post(is_gonggu=True, products=[
            {'name': 'A', 'urls': ['https://coupa.ng/1', 'https://coupa.ng/2', 'https://coupa.ng/3']}])
        post['description'] = '이 포스팅은 쿠팡파트너스 활동의 일환으로 일정액의 수수료를 제공받습니다'
        _, _, reject = transform_one(post)
        assert reject and '제휴' in reject

    def test_accepted_builds_parent_and_product_stage(self):
        # 기간/스테이지는 상품 단위로 이전됨 — 상품에 period, parent엔 is_calendar_feed.
        post = self._post(is_gonggu=True, is_calendar_feed=False,
                          products=[{'name': '냄비', 'link_location': '설명_프로필안내',
                                     'url_type': '링크모음', 'urls': ['https://litt.ly/x'],
                                     'period_start': '2026-08-01', 'period_end': '2026-08-07'}])
        parent, products, reject = transform_one(post)
        assert reject is None
        assert parent['post_id'] == 'P1'
        assert parent['is_calendar_feed'] == 0
        assert 'gonggu_stage' not in parent          # parent엔 더 이상 기간/스테이지 없음
        assert products[0]['gonggu_stage'] == '진행중'
        # 기간은 DATETIME이다(2026-08-21) — 시각 힌트가 없으면 시작 00:00:00 / 종료 23:59:59.
        assert products[0]['gonggu_start_date'] == '2026-08-01 00:00:00'
        assert products[0]['gonggu_end_date'] == '2026-08-07 23:59:59'
        assert products[0]['candidate_url'] == 'https://litt.ly/x'

    def test_calendar_feed_products_have_own_periods(self):
        # 달력 피드: 상품마다 다른 기간. is_calendar_feed=1.
        post = self._post(is_gonggu=True, is_calendar_feed=True, products=[
            {'name': '마이키즈', 'link_location': '링크없음_불명', 'urls': [],
             'period_start': '2026-08-01', 'period_end': '2026-08-15'},
            {'name': '라무르', 'link_location': '링크없음_불명', 'urls': [],
             'period_start': '2026-08-04', 'period_end': '2026-08-14'}])
        parent, products, reject = transform_one(post)
        assert reject is None
        assert parent['is_calendar_feed'] == 1
        assert products[0]['gonggu_end_date'] == '2026-08-15 23:59:59'
        assert products[1]['gonggu_start_date'] == '2026-08-04 00:00:00'
        assert products[0]['gonggu_stage'] == '진행중' and products[1]['gonggu_stage'] == '진행중'

    def test_caption_raw_becomes_parent_description(self):
        """원문 캡션은 caption_raw에서만 온다(2026-08-21). 유튜브의 description은 LLM 입력용으로
        "[제목] ..." 접두사가 붙은 가공값이라 그걸 그대로 쓰면 제목이 중복 저장된다."""
        prods = [{'name': '냄비', 'link_location': '설명_직접링크', 'urls': []}]
        ig = self._post(is_gonggu=True, products=prods)
        ig['caption_raw'] = '공구 오픈합니다\n링크는 프로필에'
        parent, _, reject = transform_one(ig)
        assert reject is None
        assert parent['description'] == '공구 오픈합니다\n링크는 프로필에'

        yt = {'platform': 'yt', 'video_id': 'V1', 'channel_id': 'c1',
              'video_url': 'https://youtu.be/V1', 'publishDate': '2026-08-01',
              'title': '역대급 공구',
              'description': '[제목] 역대급 공구\n\n본문 캡션',   # LLM 입력용 가공값
              'caption_raw': '본문 캡션',
              'classification': {'is_gonggu': True, 'products': prods}}
        parent, _, reject = transform_one(yt)
        assert reject is None
        assert parent['title'] == '역대급 공구'
        assert parent['description'] == '본문 캡션'   # 제목 접두사가 섞이지 않는다

    def test_description_none_when_caption_raw_missing(self):
        """caption_raw 도입 전에 만들어진 옛 02 레코드 — 키가 없어도 죽지 않고 None으로 두고,
        소급은 gonggu/tools/_backfill_parent_fields.py가 hifen에서 다시 읽어 채운다."""
        post = self._post(is_gonggu=True,
                          products=[{'name': '냄비', 'link_location': '설명_직접링크', 'urls': []}])
        assert 'caption_raw' not in post
        parent, _, reject = transform_one(post)
        assert reject is None
        assert parent['description'] is None
        assert parent['username'] is None          # 크리에이터 이름도 같은 취급

    def test_creator_name_carried_per_platform(self):
        """크리에이터 이름은 플랫폼별로 다른 컬럼에 담긴다 — 인스타 username(instagram_user.username),
        유튜브 channel_name(youtuber_info.title). 유튜브에서 title은 여전히 '영상' 제목이고
        채널명이 그 자리를 침범하지 않는지가 이 테스트의 핵심이다(2026-08-21)."""
        prods = [{'name': '냄비', 'link_location': '설명_직접링크', 'urls': []}]
        ig = self._post(is_gonggu=True, products=prods)
        ig['username'] = 'callmeyeal'
        parent, _, reject = transform_one(ig)
        assert reject is None
        assert parent['username'] == 'callmeyeal'
        assert 'channel_name' not in parent       # 인스타 parent에는 유튜브 컬럼이 없다

        yt = {'platform': 'yt', 'video_id': 'V1', 'channel_id': 'UC123',
              'channel_name': '슈카월드', 'title': '역대급 공구',
              'video_url': 'https://youtu.be/V1', 'publishDate': '2026-08-01',
              'caption_raw': '본문 캡션',
              'classification': {'is_gonggu': True, 'products': prods}}
        parent, _, reject = transform_one(yt)
        assert reject is None
        assert parent['channel_name'] == '슈카월드'
        assert parent['title'] == '역대급 공구'    # 영상 제목이 채널명으로 덮이지 않는다
        assert 'username' not in parent

    def test_product_period_falls_back_to_post_level(self):
        # 구 스키마(포스트 전체 period, 상품에 period 없음)도 폴백으로 각 상품에 적용.
        post = self._post(is_gonggu=True, period_start='2026-08-01', period_end='2026-08-07',
                          products=[{'name': '냄비', 'link_location': '설명_직접링크',
                                     'url_type': '없음', 'urls': []}])
        _, products, reject = transform_one(post)
        assert reject is None
        assert products[0]['gonggu_start_date'] == '2026-08-01 00:00:00'
        assert products[0]['gonggu_stage'] == '진행중'


class TestValidDt:
    """기간 DATETIME 확장(2026-08-21). 핵심은 시작/종료 기본값이 비대칭이라는 것."""

    def test_date_only_start_is_midnight(self):
        assert _valid_dt('2026-08-01') == '2026-08-01 00:00:00'

    def test_date_only_end_is_end_of_day(self):
        """종료는 23:59:59 — 그날 자정에 끝나는 게 아니라 그날 끝까지 진행되는 것이므로."""
        assert _valid_dt('2026-08-01', is_end=True) == '2026-08-01 23:59:59'

    def test_explicit_time_kept_on_both_sides(self):
        assert _valid_dt('2026-08-01 20:00') == '2026-08-01 20:00:00'
        assert _valid_dt('2026-08-01 23:00', is_end=True) == '2026-08-01 23:00:00'

    def test_explicit_midnight_end_not_overridden(self):
        """'자정 마감'이 실제 힌트로 들어온 경우엔 23:59:59로 바꾸지 않는다."""
        assert _valid_dt('2026-08-01 00:00:00', is_end=True) == '2026-08-01 00:00:00'

    def test_idempotent(self):
        once = _valid_dt('2026-08-01', is_end=True)
        assert _valid_dt(once, is_end=True) == once
        assert _valid_dt(_valid_dt('2026-08-01')) == '2026-08-01 00:00:00'

    def test_t_separator_normalized_to_space(self):
        """pymysql datetime의 .isoformat()이 내는 'T'를 공백으로 통일 — 안 하면 사전식 비교가
        뒤집힌다('T'=0x54 > ' '=0x20)."""
        assert _valid_dt('2026-08-01T20:00:00') == '2026-08-01 20:00:00'

    def test_time_without_date_is_none(self):
        """시각만 있고 날짜가 없으면 통째로 NULL — 날짜를 추측해서 채우지 않는다."""
        assert _valid_dt('20:00') is None
        assert _valid_dt('20:00', is_end=True) is None

    def test_garbage_and_null(self):
        assert _valid_dt(None) is None and _valid_dt('') is None
        assert _valid_dt('미정') is None
        assert _valid_dt('2026-13-01') is None

    def test_broken_time_falls_back_to_date_default(self):
        """시각 부분이 깨졌으면 날짜만 신뢰하고 기본 시각을 붙인다(추측 금지)."""
        assert _valid_dt('2026-08-01 99:99', is_end=True) == '2026-08-01 23:59:59'

    def test_valid_date_legacy_contract_unchanged(self):
        """옛 일회성 스크립트(scripts/_migrate_multiproduct_periods)가 쓰는 계약은 그대로."""
        assert _valid_date('2026-08-01 20:00:00') == '2026-08-01'
        assert _valid_date('2026-08-01') == '2026-08-01'


class TestComputeStageTimeAware:
    """stage 판정이 '오늘(날짜)'이 아니라 '지금(시각)' 기준이어야 한다 — 저장 정밀도만 올리는 게
    아니라 판정 로직 자체가 바뀐 부분."""

    def test_same_day_start_flips_at_the_hour(self, monkeypatch):
        """오늘 20시 오픈 공구: 19시엔 '시작전', 21시엔 '진행중'. 날짜 단위로 뭉개면 안 된다."""
        monkeypatch.delenv('GONGGU_TODAY', raising=False)
        monkeypatch.setenv('GONGGU_NOW', '2026-08-05 19:00:00')
        assert _compute_stage('2026-08-05 20:00', None) == '시작전'
        monkeypatch.setenv('GONGGU_NOW', '2026-08-05 21:00:00')
        assert _compute_stage('2026-08-05 20:00', None) == '진행중'

    def test_same_day_end_flips_at_the_hour(self, monkeypatch):
        monkeypatch.delenv('GONGGU_TODAY', raising=False)
        monkeypatch.setenv('GONGGU_NOW', '2026-08-05 22:00:00')
        assert _compute_stage(None, '2026-08-05 23:00') == '진행중'
        monkeypatch.setenv('GONGGU_NOW', '2026-08-05 23:30:00')
        assert _compute_stage(None, '2026-08-05 23:00') == '종료'

    def test_date_only_end_survives_the_whole_day(self, monkeypatch):
        """시각 힌트 없는 종료일은 그날 23:59:59까지 진행중 — 00:00:00이면 그날 내내 종료로
        뒤집혔을 것이다(이 자산이 change_period_to_datetime.sql의 보정 UPDATE와 짝이다)."""
        monkeypatch.delenv('GONGGU_TODAY', raising=False)
        monkeypatch.setenv('GONGGU_NOW', '2026-08-05 23:59:58')
        assert _compute_stage(None, '2026-08-05') == '진행중'
        monkeypatch.setenv('GONGGU_NOW', '2026-08-06 00:00:00')
        assert _compute_stage(None, '2026-08-05') == '종료'

    def test_gonggu_today_hook_still_means_midnight(self, monkeypatch):
        """옛 결정론 훅(GONGGU_TODAY)은 그 날짜의 00:00:00으로 해석 — 골든/기존 테스트 호환."""
        monkeypatch.delenv('GONGGU_NOW', raising=False)
        monkeypatch.setenv('GONGGU_TODAY', '2026-08-05')
        assert _now_iso() == '2026-08-05 00:00:00'

    def test_db_style_t_separator_input(self, monkeypatch):
        """DB에서 'T' 구분자로 직렬화된 값이 흘러와도 판정이 뒤집히지 않는다."""
        monkeypatch.delenv('GONGGU_TODAY', raising=False)
        monkeypatch.setenv('GONGGU_NOW', '2026-08-05 21:00:00')
        assert _compute_stage('2026-08-05T20:00:00', None) == '진행중'
        assert _norm_dt_str('2026-08-05T20:00:00') == '2026-08-05 20:00:00'
