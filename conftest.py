"""pytest 공통 설정 — scripts/가 아직 정식 패키지가 아니라서(대공사 2단계에서 패키지화 예정)
테스트가 `import transform`처럼 flat 임포트를 할 수 있게 sys.path에 scripts/를 얹는다.
스크립트들이 서로를 임포트하는 방식(`from common import ...`)과 정확히 같은 조건을 만든다."""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'scripts'))
