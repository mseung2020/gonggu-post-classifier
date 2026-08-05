"""pytest 공통 설정 — `pip install -e .` 없이도(저장소 루트에서 실행하는 한) gonggu 패키지가
임포트되도록 저장소 루트를 sys.path에 보장한다. 설치했다면 이 훅은 사실상 no-op."""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
