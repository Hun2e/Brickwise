"""설치 없이 src 패키지를 테스트에서 사용한다."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
