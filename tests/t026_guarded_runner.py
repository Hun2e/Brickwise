"""T-026 검증 중 비공개 평가셋 읽기와 운영 데이터 쓰기를 차단한다."""

import os
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

import pytest


ROOT = Path(__file__).resolve().parents[1]
DATA = os.path.normcase(str(ROOT / "data")) + os.sep
EVAL = os.path.normcase(str(ROOT / "eval")) + os.sep


def guarded_path(value: object) -> str:
    if not isinstance(value, (str, bytes, os.PathLike)):
        return ""
    return os.path.normcase(os.path.abspath(os.fsdecode(value)))


def audit(event: str, args: tuple) -> None:
    if event == "open":
        path = guarded_path(args[0])
        mode, flags = args[1:3]
        writing = (isinstance(mode, str) and any(char in mode for char in "wax+")) or (
            isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
        if path.startswith(EVAL) and path.endswith(".jsonl"):
            pytest.skip("사용자 지시: eval/*.jsonl 접근 차단")
        if path.startswith(DATA) and writing:
            raise PermissionError("운영 data/ 쓰기 차단")
    elif event == "sqlite3.connect":
        raw = str(args[0])
        if raw.startswith("file:"):
            parsed = urlparse(raw)
            path = unquote(parsed.path).lstrip("/") if os.name == "nt" else unquote(parsed.path)
        else:
            path = raw
        if guarded_path(path).startswith(DATA) and "mode=ro" not in raw:
            raise PermissionError("운영 SQLite는 읽기 전용 연결만 허용")
    elif event in ("os.remove", "os.rmdir", "os.mkdir", "os.rename", "os.replace", "os.truncate", "os.utime"):
        paths = args[:2] if event in ("os.rename", "os.replace") else args[:1]
        if any(guarded_path(path).startswith(DATA) for path in paths):
            raise PermissionError("운영 data/ 변경 차단")


if __name__ == "__main__":
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    os.environ.pop("GMS_KEY", None)
    sys.addaudithook(audit)
    # Chroma의 네이티브 파일 쓰기는 Python open 감사 이벤트를 거치지 않는다.
    from chromadb.config import Settings, System

    original_init = System.__init__

    def guarded_init(self: System, settings: Settings) -> None:
        if settings.is_persistent and guarded_path(settings.persist_directory).startswith(DATA):
            raise PermissionError("운영 Chroma 직접 열기 차단: 임시 복사본을 사용하세요")
        original_init(self, settings)

    System.__init__ = guarded_init
    raise SystemExit(pytest.main(sys.argv[1:]))
