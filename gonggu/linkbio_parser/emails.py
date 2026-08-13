"""링크인바이오 파싱 결과(bio/notice/sns.value/링크 제목 등 자유텍스트)에서 연락 이메일을
찾는다. resolve_links의 일일 파이프라인(runner._dump_linkbio)과 _extract_inpock_emails.py
(과거 백로그 재추출용 진단 스크립트)가 이 로직을 공유한다 — 플랫폼(inpock/linktree/littly 등)
과 무관하게 parse() 결과 dict 전체를 재귀로 훑으므로 어느 링크인바이오 서비스든 그대로 쓸 수
있다.

url/resolved_url/image 같은 링크 필드는 대상에서 뺀다(오탐 방지 — 실제로 '@'가 들어갈 일이
없는 필드들)."""
import re

_EMAIL_RE = re.compile(r'[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}')
_SKIP_KEYS = {'url', 'resolved_url', 'image', 'source_url', 'hub_url', 'key', 'background_color'}


def extract_emails(parsed, skip_keys=_SKIP_KEYS):
    """parse() 결과 dict(또는 그 일부)를 재귀로 훑어 이메일을 모두 뽑는다(등장 순서 보존,
    중복 제거)."""
    found = []
    seen = set()

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k in skip_keys:
                    continue
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
        elif isinstance(o, str):
            for m in _EMAIL_RE.findall(o):
                if m not in seen:
                    seen.add(m)
                    found.append(m)

    walk(parsed)
    return found
