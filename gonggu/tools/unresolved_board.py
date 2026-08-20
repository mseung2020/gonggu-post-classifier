#!/usr/bin/env python3
"""진행중 미해결 상품 진단 보드 — "지금 팔고 있는데 링크를 못 잡은 상품"을 한 줄씩 늘어놓고,
원본 인스타/유튜브를 바로 열어 왜 못 잡았는지 눈으로 확인하는 로컬 개발자용 창.

읽기 전용이다(SELECT만). DB에 아무것도 쓰지 않고 뷰도 만들지 않으며, daily 퀘스트에도
편입하지 않는다 — case_matrix.py와 같은 성격의 진단 도구다. 차이는 case_matrix가 "전수를
축으로 집계"하는 통계 쪽이라면, 이쪽은 "한 건씩 사람이 눈으로 보고 원인을 규명"하는 작업대라는 점.

왜 필요한가: unresolved의 원인은 통계로는 안 보인다("후보 링크 없음" 1,200건이라고 해도 그게
DM 전용 판매인지, 예고 달력인지, 링크인바이오 허브인지, 프로필에 링크가 있는데 우리가 못
따라간 것인지는 원본을 열어봐야 안다). 그 왕복(DB 조회 → id 복사 → 인스타 검색 → 캡션 읽기)을
없애서, 클릭 한 번으로 원본을 열고 캡션은 접힌 채로 이미 옆에 있게 만든 것이 이 보드다.
여기서 모은 관찰은 [[phase1-quality-feedback]] 메모의 unresolved 세부 분류를 데이터로 채우고,
결국 프롬프트/게이트/분류 제외 규칙 개선의 근거가 된다.

한 줄 구성: 플랫폼 / 마감(D-day) / 공구기간 / 공구상태 / 상품명 / link_status /
판단 이유(link_note) / 후보 도메인 / 링크위치 / 재탐색 / 원본·프로필·후보 링크. 행을 클릭하면
그 자리에서 원본 캡션 전문·분류 메모·형제 상품·재탐색 이력이 펼쳐진다.

출력은 자기완결 HTML 한 장이다(외부 CDN·서버 없음, 파일을 더블클릭하면 열림). 데이터는 HTML
안에 JSON으로 박혀 있고 검색/정렬은 브라우저에서 즉시 돈다 — 로컬 서버를 띄우지 않는
이유는 "실행 중이어야 볼 수 있는 것"보다 "언제든 열어보고 남에게 파일로 넘길 수 있는 것"이
진단 기록으로 더 쓸모 있기 때문. 최신 상태가 필요하면 그냥 다시 실행하면 된다.

실행:
    python3 -m gonggu.unresolved_board                    # 진행중 + unresolved/hold 전부
    python3 -m gonggu.unresolved_board --open             # 만들고 바로 브라우저로 열기
    python3 -m gonggu.unresolved_board --limit 50         # 소량 확인
    python3 -m gonggu.unresolved_board --status unresolved # 상태 좁히기(콤마 구분)
    python3 -m gonggu.unresolved_board --stage 진행중,시작전
    python3 -m gonggu.unresolved_board --no-caption       # hifen(SRC) 조회 생략(빠름)
    python3 -m gonggu.unresolved_board --out /tmp/b.html

분석 단위는 **상품 행 1건**이다(gonggu_post_product / gonggu_video_product). 링크 판정이
상품 단위이고 공구기간/스테이지도 2026-08-06 대공사로 상품 단위로 이전됐으므로, 진행중 필터도
부모가 아니라 상품(pp.gonggu_stage)에서 본다 — 예고 달력처럼 같은 포스트라도 상품마다 진행
상태가 다른 경우를 정확히 잡으려면 이래야 한다.
"""
import argparse
import collections
import datetime
import html
import json
import pathlib
import re
import sys
import webbrowser

from gonggu.common import ROOT, connect_dst, connect_src, load_jsonl
from gonggu.platforms import PLATFORMS
from gonggu.resolve_links.matching import product_key

OUT_FILE = ROOT / 'data/output/unresolved_board.html'

# rescan_inprogress.py의 재탐색 이력 파일. 그 모듈을 import하면 resolve_links.core(플레이라이트
# 등 무거운 크롤링 스택)까지 끌려오므로, 진단 전용인 여기서는 경로만 같은 값으로 들고 온다.
# (키 포맷은 product_key를 직접 재사용하므로 어긋날 수 없다.)
RESCAN_STATE_FILE = ROOT / 'data/output/rescan_state.jsonl'

DEFAULT_STATUSES = ('unresolved', 'hold')
DEFAULT_STAGES = ('진행중',)

# 실제 DB 값 어휘(케이스 리포트로 확인): gonggu_stage=시작전/진행중/종료/판단불가,
# link_status=NULL/done/unresolved/hold/error. 'NULL'은 "아직 resolve를 안 탄 행"을 뜻하는
# 특수 토큰으로 --status에서 받는다.
NULL_TOKEN = 'NULL'


# ------------------------------------------------------------------
# 1) SELECT — 두 플랫폼의 컬럼명 차이는 platforms.py 메타 + 아래 표에서만 흡수한다
# ------------------------------------------------------------------
# 부모 테이블에만 있고 플랫폼마다 이름이 다른(또는 한쪽에만 있는) 컬럼. 없는 쪽은 상수로
# 채워서 UNION ALL의 컬럼 수·순서를 양쪽 동일하게 유지한다.
_PARENT_EXPR = {
    'ig': {'source_url': 'p.url', 'owner_id': 'p.user_id',
           'parent_title': "''", 'external_url': 'NULL'},
    'yt': {'source_url': 'p.video_url', 'owner_id': 'p.channel_id',
           'parent_title': 'p.title', 'external_url': 'p.external_url'},
}


def _status_pred(statuses):
    """link_status 조건절. 'NULL' 토큰이 섞이면 IS NULL을 OR로 붙인다(값 목록만으로는
    미처리 행을 고를 수 없다 — MySQL에서 NULL IN (...)은 항상 참이 아님)."""
    vals = [s for s in statuses if s != NULL_TOKEN]
    parts = []
    if vals:
        quoted = ', '.join(f"'{v}'" for v in vals)
        parts.append(f'pp.link_status IN ({quoted})')
    if NULL_TOKEN in statuses:
        parts.append('pp.link_status IS NULL')
    if not parts:
        raise ValueError('--status가 비어 있습니다')
    return '(' + ' OR '.join(parts) + ')'


def _stage_pred(stages):
    quoted = ', '.join(f"'{s}'" for s in stages)
    if not stages:
        raise ValueError('--stage가 비어 있습니다')
    return f'pp.gonggu_stage IN ({quoted})'


def _select_sql(code, statuses=DEFAULT_STATUSES, stages=DEFAULT_STAGES):
    """한 플랫폼의 대상 상품 SELECT. 형제 상품 수(sib_n)와 그중 링크가 확정된 수(sib_done_n)를
    같이 세는 이유: "같은 포스트의 다른 상품은 링크를 잡았는데 이 상품만 못 잡았다"면 원인이
    포스트가 아니라 이 상품의 이름/매칭에 있다는 뜻이라, 원인 규명의 방향이 크게 달라진다."""
    p = PLATFORMS[code]
    e = _PARENT_EXPR[code]
    return f"""
SELECT '{code}' AS platform, pp.id AS product_id, p.{p.id_col} AS native_id,
       {e['owner_id']} AS owner_id, {e['source_url']} AS source_url,
       {e['parent_title']} AS parent_title, {e['external_url']} AS external_url,
       p.{p.date_col} AS publish_dt, p.is_calendar_feed, p.classification_note,
       pp.product_name, pp.link_location, pp.url_type, pp.candidate_url,
       pp.link_status, pp.link_note, pp.sort_order,
       pp.gonggu_start_date AS sd, pp.gonggu_end_date AS ed, pp.gonggu_stage AS stage,
       pp.updated_at,
       (SELECT COUNT(*) FROM {p.product_table} s
          WHERE s.{p.id_col} = pp.{p.id_col}) AS sib_n,
       (SELECT COUNT(*) FROM {p.product_table} s
          WHERE s.{p.id_col} = pp.{p.id_col} AND s.link_status = 'done') AS sib_done_n
FROM {p.product_table} pp
JOIN {p.parent_table} p ON p.{p.id_col} = pp.{p.id_col}
WHERE {_stage_pred(stages)}
  AND {_status_pred(statuses)}
"""


def _order_by():
    """종료가 임박한 순(= 지금 손쓰지 않으면 그냥 놓치는 순) → 같은 날짜면 최근 게시물 순.
    종료일이 NULL인 행은 맨 뒤로 보낸다(급한지 알 수 없으므로)."""
    return 'ORDER BY ed IS NULL, ed ASC, publish_dt DESC'


def fetch_rows(conn, statuses=DEFAULT_STATUSES, stages=DEFAULT_STAGES, limit=0):
    """두 플랫폼을 각각 조회해 하나의 리스트로 합친다. UNION ALL 한 방으로 묶지 않는 이유는
    LIMIT을 "플랫폼 합계"로 자르는 게 소량 확인에 더 자연스럽고(한쪽만 나오는 사고 방지),
    파이썬에서 정렬/자르기가 자유롭기 때문 — SELECT 자체는 싸다."""
    rows = []
    with conn.cursor() as cur:
        for code in PLATFORMS:
            cur.execute(_select_sql(code, statuses, stages) + _order_by())
            rows.extend(cur.fetchall())
    rows.sort(key=lambda r: (r['ed'] is None, r['ed'] or datetime.date.max,
                             -(r['publish_dt'].toordinal() if hasattr(r['publish_dt'], 'toordinal') else 0)))
    return rows[:limit] if limit else rows


# ------------------------------------------------------------------
# 2) 원본 캡션 / 크리에이터 프로필 링크 — hifen(SRC)에만 있다. 읽기 전용.
# ------------------------------------------------------------------
# fetch_source.py / enrich_detail/targets.py가 쓰는 것과 같은 테이블·컬럼이다. 그 두 모듈을
# import해서 재사용하지 않는 이유: fetch_source는 "최근 N일 키워드 매칭"이라 용도가 다르고,
# enrich_detail은 후반부 패키지(아직 daily 미편입)라 진단 도구가 그쪽 생명주기에 묶이면
# 곤란하다. SQL이 바뀔 일이 거의 없는 세 줄이라 여기서 따로 든다.
_CAPTION_SQL = {
    'ig': 'SELECT post_id AS k, description AS caption FROM instagram_post_description '
          'WHERE post_id IN ({ph})',
    'yt': 'SELECT video_id AS k, video_description AS caption FROM YT_video_lists_detail '
          'WHERE video_id IN ({ph})',
}
# 인스타 크리에이터의 프로필(바이오) 링크 — "프로필에 인포크가 있는데 왜 못 따라갔나"를
# 확인하려면 이게 한 줄 안에 있어야 한다. 유튜브는 부모 테이블의 external_url이 같은 역할.
_BIO_SQL = ('SELECT user_id AS k, external_url FROM instagram_user_external_url '
            'WHERE user_id IN ({ph})')
_CHUNK = 500  # IN 절이 무한정 길어지지 않게


def _in_chunks(cur, sql, keys):
    keys = sorted(k for k in keys if k)
    for i in range(0, len(keys), _CHUNK):
        chunk = keys[i:i + _CHUNK]
        cur.execute(sql.format(ph=', '.join(['%s'] * len(chunk))), chunk)
        for r in cur.fetchall():
            yield r


def fetch_src_context(rows):
    """{'caption': {(code, native_id): str}, 'bio': {user_id: [url, ...]}}.

    SRC 조회가 통째로 실패하면 경고만 찍고 빈 dict를 돌려준다 — 캡션은 보조 정보이지
    보드의 존재 이유(원본 링크로 바로 가기)가 아니므로, hifen에 못 붙어도 보드는 나와야 한다."""
    empty = {'caption': {}, 'bio': {}}
    if not rows:
        return empty
    ids = collections.defaultdict(set)
    for r in rows:
        ids[r['platform']].add(r['native_id'])
    ig_users = {r['owner_id'] for r in rows if r['platform'] == 'ig'}
    try:
        conn = connect_src()
    except Exception as e:
        print(f'  ⚠ SRC(hifen) DB 연결 실패 — 캡션 없이 보드를 만듭니다: {str(e)[:120]}',
              file=sys.stderr)
        return empty
    out = {'caption': {}, 'bio': collections.defaultdict(list)}
    try:
        with conn.cursor() as cur:
            for code in ('ig', 'yt'):
                for r in _in_chunks(cur, _CAPTION_SQL[code], ids.get(code, ())):
                    out['caption'][(code, r['k'])] = r['caption'] or ''
            for r in _in_chunks(cur, _BIO_SQL, ig_users):
                if r['external_url']:
                    out['bio'][r['k']].append(r['external_url'])
    except Exception as e:
        print(f'  ⚠ SRC(hifen) 조회 실패 — 캡션 없이 진행: {str(e)[:120]}', file=sys.stderr)
    finally:
        conn.close()
    out['bio'] = dict(out['bio'])
    return out


# ------------------------------------------------------------------
# 3) 행 가공 — 화면에 그릴 값으로 정리
# ------------------------------------------------------------------
def host_of(url):
    """도메인만 뽑는다(한 줄에 500자 URL을 다 보여줄 수 없으므로 요약용). 파싱 실패하면 빈 문자열."""
    m = re.match(r'^\s*(?:https?:)?//([^/?#\s]+)', url or '', re.I)
    return (m.group(1) if m else '').lower().replace('www.', '')


def dday_of(ed, today):
    """종료일까지 남은 일수. 종료일 미상은 None."""
    if not ed:
        return None
    if isinstance(ed, datetime.datetime):
        ed = ed.date()
    return (ed - today).days


def dday_bucket(d):
    """D-day를 "지금 급한가" 기준으로 묶는다 — 진행중인데 이미 지난 날짜면 stage 갱신이
    안 됐다는 신호라 따로 본다."""
    if d is None:
        return '종료일미상'
    if d < 0:
        return '이미지남(stage확인)'
    if d == 0:
        return '오늘마감'
    if d <= 2:
        return 'D-2이내'
    if d <= 7:
        return 'D-7이내'
    return 'D-8이상'


def period_label(sd, ed):
    """공구기간을 한 칸에 들어가는 짧은 표기로 — "08/07~08/13". 연도는 빼고(같은 해가 압도적이고
    한 줄에 자리가 없다) 전체 날짜는 title 툴팁·펼친 상세에서 본다. 한쪽만 있으면 그쪽만,
    양쪽 다 없으면 '기간미상'(= backfill_period가 못 채운 행이라는 신호)."""
    def md(v):
        if not v:
            return ''
        if isinstance(v, datetime.datetime):
            v = v.date()
        return f'{v.month:02d}/{v.day:02d}'

    a, b = md(sd), md(ed)
    if a and b:
        return f'{a}~{b}'
    if a:
        return f'{a}~?'
    if b:
        return f'?~{b}'
    return '기간미상'


def retry_info(state_rec):
    """rescan_state.jsonl 한 건을 사람이 읽는 라벨로. rescan_inprogress의 스케줄 어휘를
    그대로 따른다 — 이력 없음=아직 재탐색 안 해봄, next_due=None=백오프 소진(은퇴)."""
    if not state_rec:
        return {'retry_state': '미시도', 'retry_n': 0, 'retry_last': '', 'retry_due': ''}
    n = state_rec.get('attempts', 0)
    due = state_rec.get('next_due')
    return {
        'retry_state': '은퇴(소진)' if due is None else '대기중',
        'retry_n': n,
        'retry_last': state_rec.get('checked_at') or '',
        'retry_due': due or '',
    }


def profile_url(row):
    """크리에이터 페이지 링크. 인스타의 user_id는 원본 DB에서 숫자 PK인 경우가 있어
    instagram.com/<id>로는 안 열린다 — 숫자만이면 프로필 링크를 만들지 않고(빈 값) 대신
    바이오 링크를 보여준다."""
    if row['platform'] == 'yt':
        return f"https://www.youtube.com/channel/{row['owner_id']}" if row['owner_id'] else ''
    uid = (row['owner_id'] or '').strip()
    if not uid or uid.isdigit():
        return ''
    return f'https://www.instagram.com/{uid}/'


def shape(row, src, today):
    """DB 행 → 화면용 dict. 여기서 만든 키 이름이 그대로 HTML/JS의 컬럼 이름이다."""
    d = dday_of(row['ed'], today)
    cand = row['candidate_url'] or ''
    caption = src['caption'].get((row['platform'], row['native_id']), '')
    out = {
        'platform': row['platform'],
        'pid': row['product_id'],
        'native_id': row['native_id'],
        'owner': row['owner_id'] or '',
        'source_url': row['source_url'] or '',
        'profile_url': profile_url(row),
        'external_url': row['external_url'] or '',
        'title': row['parent_title'] or '',
        'publish': str(row['publish_dt'] or '')[:10],
        'updated': str(row['updated_at'] or '')[:10],
        'name': row['product_name'] or '',
        'status': row['link_status'] or NULL_TOKEN,
        'note': row['link_note'] or '',
        'loc': row['link_location'] or '',
        'utype': row['url_type'] or '(없음)',
        'cand': cand,
        'cand_host': host_of(cand) or ('' if cand else '(후보없음)'),
        'has_cand': '후보있음' if cand else '후보없음',
        'sd': str(row['sd'] or ''),
        'ed': str(row['ed'] or ''),
        'stage': row['stage'] or '',
        'period': period_label(row['sd'], row['ed']),
        'dday': d,
        'dday_label': ('종료일미상' if d is None
                       else ('오늘마감' if d == 0 else (f'D+{-d}' if d < 0 else f'D-{d}'))),
        'dday_bucket': dday_bucket(d),
        'sib_n': row['sib_n'],
        'sib_done_n': row['sib_done_n'],
        'sib_label': ('단독' if row['sib_n'] <= 1
                      else f"형제{row['sib_n']}(done {row['sib_done_n']})"),
        'calendar': '달력피드' if row['is_calendar_feed'] else '일반',
        'cnote': row['classification_note'] or '',
        'caption': caption,
        'bio': src['bio'].get(row['owner_id'], []) if row['platform'] == 'ig' else [],
    }
    out.update(retry_info(src.get('state', {}).get(
        product_key(row['platform'],
                    {'post_id': row['native_id'], 'video_id': row['native_id']},
                    row['sort_order']))))
    return out


# ------------------------------------------------------------------
# 4) HTML — 자기완결(외부 요청 0). 데이터는 JSON으로 박고 검색/정렬은 브라우저에서.
# ------------------------------------------------------------------
# 화면의 칩 필터는 뺐다(2026-08-10 사용자 요청 — 실데이터 1,900건에서는 url_type/link_location
# 값 종류가 많아 칩 줄이 화면 절반을 먹고, 정작 하는 일은 검색어 한 줄로 대체 가능했다).
# 대신 아래 축 목록은 터미널 요약(summarize)에서 "오늘 분포"를 찍는 용도로 그대로 쓴다 —
# 칩을 되살리고 싶으면 이 목록이 그대로 필터 정의가 된다.
SUMMARY_AXES = [
    ('platform', '플랫폼'),
    ('status', 'link_status'),
    ('has_cand', '후보링크'),
    ('utype', 'url_type'),
    ('loc', 'link_location'),
    ('dday_bucket', '마감'),
    ('retry_state', '재탐색'),
    ('calendar', '피드유형'),
    ('sib_label', '형제상품'),
    ('stage', '공구상태'),
]

SORTS = [
    ('dday', '마감 임박순'),
    ('publish', '최근 게시순'),
    ('retry_n', '재탐색 많은순'),
    ('name', '상품명순'),
]


def _json_for_script(obj):
    """<script> 안에 안전하게 박기 — '<'를 전부 유니코드 이스케이프하면 '</script>'나
    주석 시작 '<!--'이 만들어질 수 없다. 한글은 그대로 남겨 사람이 소스도 읽을 수 있게 한다."""
    return (json.dumps(obj, ensure_ascii=False, default=str)
            .replace('<', '\\u003c')
            .replace('\u2028', '\\u2028').replace('\u2029', '\\u2029'))


_CSS = """
:root{--bg:#fbfbfa;--fg:#1c1c1a;--dim:#6b6b66;--line:#e3e3df;--card:#fff;
      --hi:#2f6f4f;--warn:#a8501e;--bad:#9a2f2f;--chip:#f0f0ec;--sel:#e4efe8;}
@media (prefers-color-scheme:dark){
:root{--bg:#16171a;--fg:#e8e8e4;--dim:#9a9a94;--line:#2c2e33;--card:#1d1f23;
      --hi:#7fc4a0;--warn:#e0a06a;--bad:#e08585;--chip:#25272c;--sel:#25352c;}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:13px/1.45 -apple-system,BlinkMacSystemFont,
     "Apple SD Gothic Neo","Pretendard",system-ui,sans-serif;}
header{position:sticky;top:0;z-index:5;background:var(--bg);border-bottom:1px solid var(--line);
       padding:12px 16px 8px;}
h1{margin:0 0 2px;font-size:15px;font-weight:650;letter-spacing:-.01em}
.sub{color:var(--dim);font-size:11.5px;margin-bottom:8px}
.bar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:6px}
input[type=search]{flex:1 1 260px;min-width:180px;padding:6px 9px;border:1px solid var(--line);
       border-radius:6px;background:var(--card);color:var(--fg);font-size:13px}
select,button{padding:5px 8px;border:1px solid var(--line);border-radius:6px;background:var(--card);
       color:var(--fg);font-size:12px;cursor:pointer}
main{padding:0 16px 40px}
/* 한 줄 = 상품 1건. 열이 넘쳐서 줄바꿈되면 "한 줄로 훑는다"는 목적이 깨지므로 각 셀은
   말줄임(…)으로 자르고 전체 값은 title 툴팁과 펼친 상세에서 본다. */
/* 상품명은 좁히고 후보 도메인은 넓혔다(2026-08-10 요청) — 상품명은 대개 앞머리 몇 글자로
   식별되고 전체는 툴팁/상세에 있는데, 후보 도메인은 잘리면 판단 자체가 안 된다
   (smartstore.naver.com / link.inpock.co.kr 처럼 긴 호스트가 흔함). */
.row{display:grid;grid-template-columns:34px 62px 104px 56px 0.42fr 84px 1.05fr 268px 88px 78px 150px;
     gap:8px;align-items:baseline;padding:4px 6px;border-bottom:1px solid var(--line);cursor:pointer}
.row:hover{background:var(--card)}
.row.cur{background:var(--sel);box-shadow:inset 2px 0 0 var(--hi)}
.head{background:var(--bg);border-bottom:1px solid var(--line);
      color:var(--dim);font-size:10.5px;cursor:default;padding-top:8px}
.head:hover{background:var(--bg)}
.head span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.cell{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.name{font-weight:600}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11.5px;color:var(--dim)}
.b{display:inline-block;padding:0 5px;border-radius:4px;font-size:10.5px;border:1px solid var(--line)}
.b.ig{color:#b4479a;border-color:#b4479a55}.b.yt{color:#c33;border-color:#c3333355}
.b.unresolved{color:var(--bad);border-color:currentColor}
.b.hold{color:var(--warn);border-color:currentColor}
.b.NULL{color:var(--dim)}
.d{font-weight:650}.d.hot{color:var(--bad)}.d.warn{color:var(--warn)}.d.dim{color:var(--dim)}
.per{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11.5px}
.per.none{color:var(--dim)}
.stage{font-size:11px;color:var(--dim)}.stage.on{color:var(--hi);font-weight:600}
.acts{display:flex;gap:4px;justify-self:end}
.acts a{font-size:10.5px;text-decoration:none;color:var(--fg);border:1px solid var(--line);
        border-radius:5px;padding:1px 5px;background:var(--card);white-space:nowrap}
.acts a:hover{border-color:var(--hi);color:var(--hi)}
.det{padding:8px 10px 14px 48px;border-bottom:1px solid var(--line);background:var(--card)}
.det h4{margin:10px 0 3px;font-size:11px;color:var(--dim);font-weight:600}
.det .cap{white-space:pre-wrap;font-size:12.5px;max-height:44vh;overflow:auto;
          border-left:2px solid var(--line);padding-left:9px}
.det .kv{font-size:11.5px;color:var(--dim)}
.det a{color:var(--hi);word-break:break-all}
.empty{padding:40px 0;color:var(--dim);text-align:center}
footer{padding:10px 16px 30px;color:var(--dim);font-size:11px;border-top:1px solid var(--line)}
kbd{border:1px solid var(--line);border-bottom-width:2px;border-radius:4px;padding:0 4px;
    font-size:10.5px;font-family:inherit;background:var(--chip)}
"""

_JS = r"""
const $ = s => document.querySelector(s);
const state = {q:'', sort:'dday', open:new Set(), cur:-1};
const norm = s => (s||'').toString().toLowerCase();

// 검색은 "한 줄에 보이는 것 + 캡션/분류메모/URL"을 통째로 훑는다 — 진단 중엔 "인포크"나
// "DM" 같은 단어로 캡션을 뒤지는 게 가장 잦은 동작이라 캡션을 검색 대상에 꼭 넣는다.
// 칩 필터를 없앤 뒤로는 검색이 유일한 좁히기 수단이라, 예전에 칩으로 걸던 값(link_status,
// url_type, link_location, stage, 재탐색 상태, 달력피드 여부…)도 전부 건초더미에 넣는다 —
// "hold 링크없음_불명"처럼 두 단어를 띄어 쓰면 AND로 걸려 칩 두 개를 누른 것과 같아진다.
const HAY = r => norm([r.name, r.note, r.cnote, r.caption, r.cand, r.owner, r.native_id,
                       r.status, r.utype, r.loc, r.title, r.stage, r.period, r.dday_label,
                       r.retry_state, r.calendar, r.has_cand, r.sib_label,
                       (r.bio||[]).join(' ')].join(' \u0001 '));

function filtered(){
  const q = state.q.trim().toLowerCase();
  const terms = q ? q.split(/\s+/) : [];
  let out = ROWS.filter(r => {
    if (!terms.length) return true;
    const h = HAY(r);
    return terms.every(t => h.includes(t));
  });
  const s = state.sort;
  out.sort((a,b) => {
    if (s === 'dday'){
      const av = a.dday === null ? 99999 : a.dday, bv = b.dday === null ? 99999 : b.dday;
      return av - bv || String(b.publish).localeCompare(String(a.publish));
    }
    if (s === 'publish') return String(b.publish).localeCompare(String(a.publish));
    if (s === 'retry_n') return b.retry_n - a.retry_n;
    return String(a.name).localeCompare(String(b.name), 'ko');
  });
  return out;
}

const esc = s => (s ?? '').toString()
  .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
const dcls = r => r.dday === null ? 'dim' : (r.dday <= 0 ? 'hot' : (r.dday <= 2 ? 'warn' : ''));

function link(url, label, title){
  if (!url) return '';
  return `<a href="${esc(url)}" target="_blank" rel="noreferrer" title="${esc(title||url)}"
          onclick="event.stopPropagation()">${label}</a>`;
}

function rowHTML(r, i){
  // 라벨은 짧게(줄바꿈되면 한 줄이 두 줄이 된다), 어디로 가는지는 title 툴팁으로.
  const srcTip = (r.platform === 'ig' ? '인스타그램 게시물' : '유튜브 영상') + ' 열기';
  return `<div class="row${state.cur===i?' cur':''}" data-i="${i}">
    <span class="cell b ${r.platform}">${r.platform.toUpperCase()}</span>
    <span class="cell d ${dcls(r)}" title="공구 ${esc(r.sd||'?')} ~ ${esc(r.ed||'?')}">${esc(r.dday_label)}</span>
    <span class="cell per${r.period === '기간미상' ? ' none' : ''}"
          title="공구기간 ${esc(r.sd||'?')} ~ ${esc(r.ed||'?')}">${esc(r.period)}</span>
    <span class="cell stage${r.stage === '진행중' ? ' on' : ''}" title="gonggu_stage(상품 단위)">${esc(r.stage||'-')}</span>
    <span class="cell name" title="${esc(r.name)}">${esc(r.name)}</span>
    <span class="cell b ${r.status}">${esc(r.status)}</span>
    <span class="cell mono" title="${esc(r.note)}">${esc(r.note || '—')}</span>
    <span class="cell mono" title="${esc(r.cand || r.loc)}">${esc(r.cand_host || r.utype)}</span>
    <span class="cell mono" title="link_location">${esc(r.loc)}</span>
    <span class="cell mono" title="재탐색 ${r.retry_n}회 / 다음 ${esc(r.retry_due||'-')}">${esc(r.retry_state)}${r.retry_n?('·'+r.retry_n):''}</span>
    <span class="acts">
      ${link(r.source_url, '↗원본', srcTip)}
      ${link(r.profile_url || (r.bio||[])[0] || r.external_url, '↗프로필', '크리에이터/프로필 링크')}
      ${link(r.cand, '↗후보', r.cand)}
    </span></div>`;
}

function detHTML(r){
  const bio = (r.bio||[]).map(u => link(u, esc(u), u)).join('<br>');
  return `<div class="det">
    <div class="kv">${esc(r.platform)} · ${esc(r.native_id)} · product_id ${r.pid} ·
      게시 ${esc(r.publish)} · 공구 ${esc(r.sd||'?')} ~ ${esc(r.ed||'?')} (${esc(r.stage)}) ·
      ${esc(r.sib_label)} · ${esc(r.calendar)} · 상품행 갱신 ${esc(r.updated)} ·
      재탐색 ${r.retry_n}회(${esc(r.retry_state)}${r.retry_last?', 최근 '+esc(r.retry_last):''}${r.retry_due?', 다음 '+esc(r.retry_due):''})</div>
    ${r.title ? `<h4>영상 제목</h4><div>${esc(r.title)}</div>` : ''}
    <h4>link_note (resolve가 이 상태로 판단한 이유)</h4><div>${esc(r.note || '—')}</div>
    <h4>classification_note (LLM#1 분류 메모)</h4><div>${esc(r.cnote || '—')}</div>
    <h4>후보 URL</h4><div>${r.cand ? link(r.cand, esc(r.cand), r.cand) : '—'}
      &nbsp;<span class="kv">url_type ${esc(r.utype)} / link_location ${esc(r.loc)}</span></div>
    ${bio ? `<h4>크리에이터 프로필 링크(hifen)</h4><div>${bio}</div>` : ''}
    ${r.external_url ? `<h4>채널 정보란 링크</h4><div>${link(r.external_url, esc(r.external_url))}</div>` : ''}
    <h4>원본 캡션</h4><div class="cap">${esc(r.caption) || '(캡션 없음 — hifen 조회 실패이거나 원문이 빈 값)'}</div>
  </div>`;
}

let VIEW = [];
// 필터에 걸린 행을 처음부터 전부 그린다(2026-08-10 요청 — 예전엔 200건씩 "더 보기"였다).
// 실측: 상품 2,000건이면 문자열 한 방 innerHTML으로 수십 ms, 스크롤도 부담 없다. 대신
// "전체 펼치기"는 캡션 전문을 그만큼 밀어넣으므로 건수가 많을 때만 느려질 수 있다.
function render(){
  VIEW = filtered();
  const list = $('#list');
  let h = `<div class="row head">
    <span>플랫폼</span><span>마감</span><span>공구기간</span><span>공구상태</span>
    <span>상품명</span><span>link_status</span><span>판단 이유(link_note)</span>
    <span>후보 도메인</span><span>링크위치</span><span>재탐색</span>
    <span style="justify-self:end">열기</span></div>`;
  VIEW.forEach((r,i) => { h += rowHTML(r,i); if (state.open.has(r.pid)) h += detHTML(r); });
  if (!VIEW.length) h += `<div class="empty">조건에 맞는 상품이 없습니다.</div>`;
  list.innerHTML = h;
  $('#count').textContent = `${VIEW.length.toLocaleString()}건 표시 / 전체 ${ROWS.length.toLocaleString()}건`;
  list.querySelectorAll('.row[data-i]').forEach(el => {
    el.onclick = () => { const i = +el.dataset.i; state.cur = i; expand(VIEW[i]); };
  });
}

function expand(r){
  if (!r) return;
  state.open.has(r.pid) ? state.open.delete(r.pid) : state.open.add(r.pid);
  render();
}

document.addEventListener('keydown', e => {
  if (['INPUT','SELECT','TEXTAREA'].includes(e.target.tagName)){
    if (e.key === 'Escape') e.target.blur();
    return;
  }
  if (e.key === '/'){ e.preventDefault(); $('#q').focus(); return; }
  if (e.key === 'j' || e.key === 'ArrowDown'){ state.cur = Math.min(state.cur+1, VIEW.length-1); }
  else if (e.key === 'k' || e.key === 'ArrowUp'){ state.cur = Math.max(state.cur-1, 0); }
  else if (e.key === 'Enter' || e.key === ' '){ e.preventDefault(); expand(VIEW[state.cur]); return; }
  else if (e.key === 'o'){ const r = VIEW[state.cur]; if (r && r.source_url) window.open(r.source_url,'_blank'); return; }
  else if (e.key === 'p'){ const r = VIEW[state.cur];
    const u = r && (r.profile_url || (r.bio||[])[0] || r.external_url); if (u) window.open(u,'_blank'); return; }
  else return;
  e.preventDefault(); render();
  const cur = document.querySelector('.row.cur');
  if (cur) cur.scrollIntoView({block:'nearest'});
});

// 전체 행을 한 번에 그리므로(더보기 없음) 2,000건에서 render() 한 번이 100~200ms다 —
// 타이핑마다 그리면 한 글자씩 밀리는 느낌이 나서 입력만 짧게 디바운스한다.
let qTimer = null;
$('#q').oninput = e => {
  const v = e.target.value;
  clearTimeout(qTimer);
  qTimer = setTimeout(() => { state.q = v; render(); }, 120);
};
$('#sort').onchange = e => { state.sort = e.target.value; render(); };
$('#reset').onclick = () => { state.q = ''; $('#q').value = ''; render(); };
$('#expandAll').onclick = () => {
  const on = state.open.size === 0;
  state.open = on ? new Set(VIEW.map(r => r.pid)) : new Set();
  render();
};
render();
"""

_HTML = """<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>{css}</style></head>
<body>
<header>
  <h1>{title}</h1>
  <div class="sub">{subtitle}</div>
  <div class="bar">
    <input id="q" type="search" placeholder="상품명·캡션·판단이유·URL 검색 (여러 단어=AND,  / 로 포커스)">
    <select id="sort">{sort_options}</select>
    <button id="expandAll">전체 펼치기/접기</button>
    <button id="reset">검색 초기화</button>
    <span class="sub" id="count"></span>
  </div>
</header>
<main><div id="list"></div></main>
<footer>
  단축키 <kbd>j</kbd>/<kbd>k</kbd> 이동 · <kbd>Enter</kbd> 펼치기 · <kbd>o</kbd> 원본 열기 ·
  <kbd>p</kbd> 프로필 열기 · <kbd>/</kbd> 검색<br>
  읽기 전용 스냅샷입니다 — DB를 바꾸지 않으며, 최신 상태가 필요하면
  <code>python3 -m gonggu.unresolved_board</code>를 다시 실행하세요.
</footer>
<script>
const ROWS = {rows};
{js}
</script>
</body></html>
"""


def render_html(rows, statuses=DEFAULT_STATUSES, stages=DEFAULT_STAGES, generated_at=None):
    gen = generated_at or datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
    sub = (f"기준 {gen} · 상품 {len(rows):,}건 · "
           f"stage {'/'.join(stages)} · link_status {'/'.join(statuses)} · "
           f"한 줄 = 상품 1건(읽기 전용)")
    opts = ''.join(f'<option value="{k}">{html.escape(v)}</option>' for k, v in SORTS)
    return _HTML.format(
        title='진행중 미해결 상품 진단 보드',
        subtitle=html.escape(sub),
        css=_CSS, js=_JS, sort_options=opts,
        rows=_json_for_script(rows),
    )


# ------------------------------------------------------------------
# 5) CLI
# ------------------------------------------------------------------
def summarize(rows, top=6):
    """터미널에 축별 분포를 찍어서 HTML을 열기 전에 "오늘은 어떤 상태인지" 감을 준다 —
    화면에서 칩 필터를 뺐으므로 분포를 보는 창구는 여기다(더 깊은 집계는 case_matrix)."""
    lines = []
    for key, label in SUMMARY_AXES:
        c = collections.Counter(str(r.get(key, '')) for r in rows)
        head = ', '.join(f'{k}={v:,}' for k, v in c.most_common(top))
        lines.append(f'  - {label}: {len(c)}종 — {head}{" ..." if len(c) > top else ""}')
    return lines


def _csv_list(s):
    return tuple(x.strip() for x in (s or '').split(',') if x.strip())


def main():
    ap = argparse.ArgumentParser(
        description='진행중 미해결 상품 진단 보드 HTML 생성(읽기 전용)')
    ap.add_argument('--status', default=','.join(DEFAULT_STATUSES),
                    help=f"대상 link_status 콤마 구분(기본 {','.join(DEFAULT_STATUSES)}, "
                         f"미처리 행은 {NULL_TOKEN})")
    ap.add_argument('--stage', default=','.join(DEFAULT_STAGES),
                    help=f"대상 gonggu_stage 콤마 구분(기본 {','.join(DEFAULT_STAGES)})")
    ap.add_argument('--limit', type=int, default=0, help='상품 N건만(소량 확인용)')
    ap.add_argument('--no-caption', action='store_true',
                    help='hifen(SRC) 캡션·프로필 링크 조회를 생략(빠름, 링크로만 확인)')
    ap.add_argument('--out', metavar='PATH', help=f'출력 경로(기본 {OUT_FILE})')
    ap.add_argument('--open', action='store_true', dest='do_open',
                    help='생성 후 기본 브라우저로 열기')
    args = ap.parse_args()

    statuses, stages = _csv_list(args.status), _csv_list(args.stage)
    conn = connect_dst()
    try:
        rows = fetch_rows(conn, statuses, stages, args.limit)
    finally:
        conn.close()

    src = {'caption': {}, 'bio': {}} if args.no_caption else fetch_src_context(rows)
    # 재탐색 이력은 파일이라 없어도 정상(아직 rescan을 안 돌린 저장소) — 그때는 전부 '미시도'.
    src['state'] = load_jsonl(RESCAN_STATE_FILE)

    today = datetime.date.today()
    shaped = [shape(r, src, today) for r in rows]

    out = pathlib.Path(args.out) if args.out else OUT_FILE
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_html(shaped, statuses, stages), encoding='utf-8')

    print(f'진행중 미해결 상품 {len(shaped):,}건 (stage {"/".join(stages)}, '
          f'link_status {"/".join(statuses)})')
    for line in summarize(shaped):
        print(line)
    if not args.no_caption:
        got = sum(1 for r in shaped if r['caption'])
        print(f'  - 캡션 확보: {got:,}/{len(shaped):,}건')
    print(f'출력: {out}')
    if args.do_open:
        webbrowser.open(out.resolve().as_uri())


if __name__ == '__main__':
    main()
