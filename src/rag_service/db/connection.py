"""리포 루트 기준 SQLite 연결."""

import sqlite3
from pathlib import Path

from rag_service.config import resolve_path


def connect(path: str | Path) -> sqlite3.Connection:
    """연결을 반환한다. 호출자가 commit/rollback 및 close를 담당한다."""
    conn = sqlite3.connect(resolve_path(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn
