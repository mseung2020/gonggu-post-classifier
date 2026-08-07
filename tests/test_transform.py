"""transform.py 순수 함수 단위 테스트 — 게이트 규칙의 경계 사례들을 박제한다."""
import pytest

from gonggu.transform import _compute_stage, _product_row, _valid_date, transform_one


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
        assert products[0]['gonggu_start_date'] == '2026-08-01'
        assert products[0]['gonggu_end_date'] == '2026-08-07'
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
        assert products[0]['gonggu_end_date'] == '2026-08-15'
        assert products[1]['gonggu_start_date'] == '2026-08-04'
        assert products[0]['gonggu_stage'] == '진행중' and products[1]['gonggu_stage'] == '진행중'

    def test_product_period_falls_back_to_post_level(self):
        # 구 스키마(포스트 전체 period, 상품에 period 없음)도 폴백으로 각 상품에 적용.
        post = self._post(is_gonggu=True, period_start='2026-08-01', period_end='2026-08-07',
                          products=[{'name': '냄비', 'link_location': '설명_직접링크',
                                     'url_type': '없음', 'urls': []}])
        _, products, reject = transform_one(post)
        assert reject is None
        assert products[0]['gonggu_start_date'] == '2026-08-01'
        assert products[0]['gonggu_stage'] == '진행중'
