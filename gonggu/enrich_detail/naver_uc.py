"""(공용화됨, 2026-08-07) 네이버 uc 엔진은 gonggu.uc_engine으로 옮겨져 enrich_detail과
resolve_links가 공유한다. 이 모듈은 기존 import 경로(gonggu.enrich_detail.naver_uc)를 그대로
유지하는 재수출 shim이다 — 드라이버 싱글턴/락도 uc_engine 것을 공유하므로 두 경로가 같은 크롬
창 하나를 직렬로 나눠 쓴다(같은 프로필·신뢰 쿠키, 동시 창 없음).

새 코드는 gonggu.uc_engine에서 직접 import하는 걸 권장한다."""
from gonggu.uc_engine import (  # noqa: F401
    CHALLENGE_MARKERS,
    DEFAULT_PROFILE,
    build_driver,
    close_sync,
    fetch_sync,
    looks_challenged,
)
