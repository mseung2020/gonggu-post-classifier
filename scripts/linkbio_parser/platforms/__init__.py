"""플랫폼별 파서. 각 모듈은 parse(url, resolve_links=True) -> dict 형태를 따른다(전략 패턴 —
registry.py의 PARSERS 표가 플랫폼 이름으로 알맞은 모듈의 parse()를 골라 호출한다)."""
