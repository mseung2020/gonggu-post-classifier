"""호환 shim(2026-08-20 리포 정리) — 실제 코드는 gonggu/infra/common.py로 옮겼다.

CLI로 실행되지 않는(항상 import만 되는) 모듈이라 __main__ 분기가 필요 없다. 새 코드는
gonggu/infra/common.py에 쓸 것."""
import sys

from gonggu.infra import common as _real

sys.modules[__name__] = _real
