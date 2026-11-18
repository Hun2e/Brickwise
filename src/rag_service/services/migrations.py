"""사용자 DB를 백업한 뒤 한 트랜잭션으로 비파괴 이관한다."""

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from rag_service.config import load_settings, resolve_path
from rag_service.services.dialogue_state import DialogueState, Subject, validate_set_num

USER_SCHEMA_VERSION = 1
_SCHEMA = Path(__file__).resolve().parents[1] / "db/user_schema.sql"
_COLUMNS = {
    "conversation_turns": {
        "conversation_id": "TEXT REFERENCES conversations(id) ON DELETE CASCADE",
        "request_id": "TEXT",
        "kind": "TEXT NOT NULL DEFAULT 'answer'",
        "state_version": "INTEGER",
    },
    "collections": {"version": "INTEGER NOT NULL DEFAULT 0"},
}
_INDEXES = {
    "idx_turns_request": "CREATE UNIQUE INDEX idx_turns_request ON conversation_turns(request_id)",
    "idx_turns_conversation": "CREATE INDEX idx_turns_conversation ON conversation_turns(conversation_id, id)",
}
_TABLES = {"collections", "collection_sets", "collection_adjustments", "conversation_turns",
           "schema_meta", "conversations", "conversation_state", "turn_subjects", "chat_requests",
           "collection_events"}


class MigrationError(Exception):
    """내부 DB 오류 대신 사용자에게 표시할 고정 안내."""


@dataclass(frozen=True)
class MigrationReport:
    backup_path: Path | None = None
    changes: int = 0
    conversations_created: int = 0
    turns_migrated: int = 0
    version: int = USER_SCHEMA_VERSION


def _objects(conn: sqlite3.Connection, kind: str) -> set[str]:
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type=?", (kind,))}


def _version(conn: sqlite3.Connection) -> int:
    if "schema_meta" not in _objects(conn, "table"):
        return 0
    row = conn.execute("SELECT value FROM schema_meta WHERE key='user_schema_version'").fetchone()
    return int(row[0]) if row else 0


def _needs_migration(conn: sqlite3.Connection) -> bool:
    version = _version(conn)
    if version > USER_SCHEMA_VERSION:
        raise MigrationError("대화 저장소가 더 새 버전입니다. 프로그램 버전을 확인하세요.")
    if version != USER_SCHEMA_VERSION or not _TABLES <= _objects(conn, "table"):
        return True
    for table, columns in _COLUMNS.items():
        if not columns.keys() <= {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}:
            return True
    if not _INDEXES.keys() <= _objects(conn, "index"):
        return True
    return conn.execute("SELECT 1 FROM conversation_turns WHERE conversation_id IS NULL LIMIT 1").fetchone() is not None


def migration_needed(path: str | Path) -> bool:
    """파일을 생성하거나 스키마를 바꾸지 않는 준비 상태 조회."""
    path = Path(path).resolve()
    if not path.exists():
        return True
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
            return _needs_migration(conn)
    except (sqlite3.Error, OSError, ValueError):
        raise MigrationError("대화 저장소 이관 상태를 확인하지 못했습니다. DB와 백업을 확인하세요.") from None


def _backup(path: Path) -> Path:
    directory = path.parent / "backup"
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = directory / f"user-{stamp}-{uuid4().hex}.sqlite"
    # SQLite backup은 WAL에 남아 있는 커밋까지 포함한다.
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as source:
        with closing(sqlite3.connect(target)) as destination:
            source.backup(destination)
    return target


def _legacy_title(thread: str) -> str:
    if thread in ("explore", "collection"):
        return {"explore": "탐색(이전)", "collection": "내 컬렉션(이전)"}[thread]
    try:
        path = resolve_path(load_settings()["paths"]["sqlite"])
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
            row = conn.execute("SELECT name FROM sets WHERE set_num=?", (thread,)).fetchone()
        if row:
            return f"{thread} {row[0]}"
    except (sqlite3.Error, OSError, ValueError, KeyError):
        pass
    return thread


def _legacy_subject(thread: str) -> Subject | None:
    try:
        validate_set_num(thread)
    except ValueError:
        return None
    return Subject(thread, "ui", None)


def _import_legacy(conn: sqlite3.Connection) -> tuple[int, int]:
    created = migrated = 0
    groups = conn.execute(
        "SELECT uid, thread, MIN(created_at), MAX(created_at) FROM conversation_turns "
        "WHERE conversation_id IS NULL GROUP BY uid, thread"
    ).fetchall()
    for uid, thread, first, last in groups:
        row = conn.execute("SELECT id FROM conversations WHERE uid=? AND legacy_thread=?", (uid, thread)).fetchone()
        subject = _legacy_subject(thread)
        if row:
            cid = row[0]
        else:
            cid = uuid4().hex
            conn.execute("INSERT INTO conversations VALUES (?, ?, ?, ?, ?, ?)",
                         (cid, uid, _legacy_title(thread), first, last, thread))
            conn.execute("INSERT INTO conversation_state VALUES (?, ?, 0, ?)",
                         (cid, DialogueState(subject=subject).to_json(), last))
            created += 1
        if subject:
            conn.execute(
                "INSERT OR IGNORE INTO turn_subjects(turn_id, set_num, role) "
                "SELECT id, ?, 'primary' FROM conversation_turns "
                "WHERE uid=? AND thread=? AND conversation_id IS NULL", (thread, uid, thread),
            )
        migrated += conn.execute(
            "UPDATE conversation_turns SET conversation_id=?, state_version=0 "
            "WHERE uid=? AND thread=? AND conversation_id IS NULL", (cid, uid, thread),
        ).rowcount
        conn.execute("UPDATE conversations SET updated_at=MAX(updated_at, ?) WHERE id=? AND updated_at < ?",
                     (last, cid, last))
    return created, migrated


def migrate(path: str | Path) -> MigrationReport:
    """명시적으로만 실행한다. 실패 시 스키마와 데이터 변경을 모두 롤백한다."""
    path = Path(path).resolve()
    existed = path.exists()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("BEGIN IMMEDIATE")
            if not _needs_migration(conn):
                return MigrationReport()
            backup = _backup(path) if existed else None
            before_objects = set(conn.execute("SELECT type, name FROM sqlite_master"))
            before_changes = conn.total_changes
            # executescript의 암묵적 커밋을 피해야 ALTER와 이관도 함께 롤백된다.
            for statement in _SCHEMA.read_text(encoding="utf-8").split(";"):
                if statement.strip():
                    conn.execute(statement)
            columns_added = 0
            for table, columns in _COLUMNS.items():
                present = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
                for column, definition in columns.items():
                    if column not in present:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
                        columns_added += 1
            present_indexes = _objects(conn, "index")
            for name, statement in _INDEXES.items():
                if name not in present_indexes:
                    conn.execute(statement)
            created, migrated = _import_legacy(conn)
            conn.execute("INSERT INTO schema_meta(key, value) VALUES ('user_schema_version', ?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value WHERE value != excluded.value",
                         (str(USER_SCHEMA_VERSION),))
            objects_added = len(set(conn.execute("SELECT type, name FROM sqlite_master")) - before_objects)
            changes = conn.total_changes - before_changes + columns_added + objects_added
        return MigrationReport(backup, changes, created, migrated)
    except (sqlite3.Error, OSError, ValueError):
        raise MigrationError("대화 저장소 이관에 실패했습니다. DB 권한과 백업을 확인한 뒤 다시 실행하세요.") from None
