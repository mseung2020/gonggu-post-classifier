"""호환 shim(2026-08-20 리포 정리) — 실제 코드는 gonggu/pipeline/uc_healthcheck.py로 옮겼다.

`python3 -m gonggu.uc_healthcheck` 같은 예전 호출과 `from gonggu.uc_healthcheck import X` 같은 예전 import가
전부 그대로 동작하게 하는 게 이 파일의 유일한 역할이다 — 옮긴 뒤 손댈 파일이 아니다.
새 코드는 gonggu/pipeline/uc_healthcheck.py에 쓸 것."""
import sys

from gonggu.pipeline import uc_healthcheck as _real

if __name__ != '__main__':
    # 일반 import(`import gonggu.uc_healthcheck`, `from gonggu.uc_healthcheck import X`, pyproject.toml의
    # `gonggu.uc_healthcheck:main` 진입점 포함)는 sys.modules 자체를 실제 모듈로 바꿔치기한다 — `import *`와
    # 달리 밑줄로 시작하는 이름까지 그대로 넘어가므로 테스트가 내부 헬퍼를 직접 import해도 안전하다.
    sys.modules[__name__] = _real
else:
    # `python3 -m gonggu.uc_healthcheck`로 실행된 경우 — 위 스왑은 sys.modules['__main__']을 덮어써서
    # 실행 중인 프로세스를 망가뜨리므로 하지 않고, 대신 실제 main()을 직접 부른다.
    _real.main()
