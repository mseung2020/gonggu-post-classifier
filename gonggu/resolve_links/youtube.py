"""유튜브 전용 링크 복구 — 캡션에 없는/잘린 링크를 유튜브 자체 페이지에서 긁어와 채운다."""
import json
import re
import threading

import requests

from .config import UA


def _find_channel_links(obj):
    """유튜브 채널 '정보' 탭 페이지의 ytInitialData 안에서 channelExternalLinkViewModel을
    깊이 상관없이 재귀적으로 찾는다 — 정확한 중첩 경로에 의존하면 유튜브가 내부 구조를
    바꿀 때마다 깨지기 쉬워서, 키 이름만 보고 어디에 있든 찾아낸다."""
    found = []
    if isinstance(obj, dict):
        v = obj.get('channelExternalLinkViewModel')
        if isinstance(v, dict):
            link = ((v.get('link') or {}).get('content') or '').strip()
            if link:
                found.append(link)
        for val in obj.values():
            found += _find_channel_links(val)
    elif isinstance(obj, list):
        for item in obj:
            found += _find_channel_links(item)
    return found


_CHANNEL_LINK_CACHE = {}
_CHANNEL_LINK_LOCK = threading.Lock()


def youtube_channel_link(channel_id):
    """유튜브 채널 '정보' 탭엔 캡션과 별개로 채널 전용 링크 필드가 있다(hifen SRC_DB의
    YT_channel* 테이블엔 URL 컬럼 자체가 없어서 DB에서는 못 가져옴 — 실측 확인 2026-07-20,
    goodday_000 채널 스크린샷 참고). 채널당 한 번만 긁어서 캐싱한다 — 같은 채널의 영상이
    여러 개 걸릴 수 있고, 유튜브는 인포크보다 크롤링 감시가 엄격해서 요청 수를 최소화해야
    한다."""
    with _CHANNEL_LINK_LOCK:
        if channel_id in _CHANNEL_LINK_CACHE:
            return _CHANNEL_LINK_CACHE[channel_id]
    url = None
    try:
        resp = requests.get(f'https://www.youtube.com/channel/{channel_id}/about',
                             headers={'User-Agent': UA}, timeout=15)
        m = re.search(r'var ytInitialData = (\{.*?\});</script>', resp.text, re.S)
        if m:
            links = _find_channel_links(json.loads(m.group(1)))
            if links:
                raw = links[0]
                url = raw if raw.startswith('http') else f'https://{raw}'
    except Exception:
        url = None
    with _CHANNEL_LINK_LOCK:
        _CHANNEL_LINK_CACHE[channel_id] = url
    return url


_VIDEO_DESC_CACHE = {}
_VIDEO_DESC_LOCK = threading.Lock()


def _youtube_full_description(video_id):
    """hifen SRC_DB의 YT_video_lists_detail.video_description은 특정 URL만 '...'으로
    잘려서 저장돼 있는 경우가 있다(중간 가공 단계에서 그런 것으로 추정 — 실측 확인,
    2026-07-20: godomall.com URL이 잘림). 근데 유튜브 watch 페이지의
    ytInitialPlayerResponse.videoDetails.shortDescription엔 안 잘린 원문 전체가 있다.
    영상당 한 번만 긁어서 캐싱한다."""
    with _VIDEO_DESC_LOCK:
        if video_id in _VIDEO_DESC_CACHE:
            return _VIDEO_DESC_CACHE[video_id]
    desc = None
    try:
        resp = requests.get(f'https://www.youtube.com/watch?v={video_id}',
                             headers={'User-Agent': UA}, timeout=15)
        m = re.search(r'var ytInitialPlayerResponse = (\{.*?\});', resp.text, re.S)
        if m:
            desc = json.loads(m.group(1)).get('videoDetails', {}).get('shortDescription') or None
    except Exception:
        desc = None
    with _VIDEO_DESC_LOCK:
        _VIDEO_DESC_CACHE[video_id] = desc
    return desc


def recover_truncated_url(video_id, truncated_url):
    """잘린 candidate_url('...' 앞부분)로 시작하는 완전한 URL을 원문 설명에서 찾아 복구한다."""
    prefix = truncated_url.split('...')[0]
    desc = _youtube_full_description(video_id)
    if not desc or prefix not in desc:
        return None
    rest = desc[desc.index(prefix):]
    m = re.match(r'\S+', rest)
    return m.group(0) if m else None
