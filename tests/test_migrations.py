import shutil
import sqlite3
from contextlib import closing

import pytest

from rag_service.services import migrations, status
from rag_service.services.conversations import ConversationRepo, ConversationStore
from rag_service.services.dialogue_state import DialogueState

UID = "a" * 32
OTHER = "b" * 32
LEGACY_SCHEMA = """
CREATE TABLE collections(id TEXT PRIMARY KEY);
CREATE TABLE collection_sets(collection_id TEXT, set_num TEXT, qty INTEGER,
    PRIMARY KEY(collection_id, set_num));
CREATE TABLE collection_adjustments(id INTEGER PRIMARY KEY, collection_id TEXT,
    part_num TEXT, color_id INTEGER, delta INTEGER);
CREATE TABLE conversation_turns(id INTEGER PRIMARY KEY AUTOINCREMENT,
    uid TEXT NOT NULL, thread TEXT NOT NULL, question TEXT NOT NULL,
    response TEXT NOT NULL, created_at TEXT NOT NULL);
"""


@pytest.fixture
def legacy_copy(tmp_path, monkeypatch):
    original = tmp_path / "original.sqlite"
    with closing(sqlite3.connect(original)) as conn, conn:
        conn.executescript(LEGACY_SCHEMA)
        conn.execute("INSERT INTO collections VALUES (?)", (UID,))
        conn.execute("INSERT INTO collection_sets VALUES (?, 'A-1', 2)", (UID,))
        conn.execute("INSERT INTO collection_adjustments VALUES (1, ?, 'part', 1, -1)", (UID,))
        for i, (uid, thread) in enumerate([(UID, "A-1"), (UID, "A-1"), (UID, "B-1"),
                                           (UID, "explore"), (UID, "collection"), (OTHER, "A-1")]):
            conn.execute("INSERT INTO conversation_turns(uid, thread, question, response, created_at) "
                         "VALUES (?, ?, ?, ?, ?)", (uid, thread, f"질문 {i}", '{"answer":"원문"}', f"2026-01-01T00:00:0{i}Z"))
    reference = tmp_path / "lego.sqlite"
    with closing(sqlite3.connect(reference)) as conn, conn:
        conn.executescript("CREATE TABLE sets(set_num TEXT, name TEXT); INSERT INTO sets VALUES ('A-1', 'Alpha');")
    monkeypatch.setattr(migrations, "load_settings", lambda: {"paths": {"sqlite": str(reference)}})
    target = tmp_path / "copy.sqlite"
    shutil.copy2(original, target)
    return original, target


def legacy_rows(path):
    with closing(sqlite3.connect(path)) as conn:
        return {table: conn.execute(query).fetchall() for table, query in {
            "collections": "SELECT id FROM collections",
            "sets": "SELECT * FROM collection_sets",
            "adjustments": "SELECT * FROM collection_adjustments",
            "turns": "SELECT id, uid, thread, question, response, created_at FROM conversation_turns",
        }.items()}


def dump(path):
    with closing(sqlite3.connect(path)) as conn:
        return list(conn.iterdump())


def test_backup_idempotence_and_legacy_isolation(legacy_copy):
    original, path = legacy_copy
    untouched = original.read_bytes()
    before = legacy_rows(original)
    assert migrations.migration_needed(path)
    report = migrations.migrate(path)
    assert report.changes > 0 and report.turns_migrated == 6 and report.conversations_created == 5
    assert report.backup_path.parent == path.parent / "backup"
    assert legacy_rows(report.backup_path) == before == legacy_rows(path)
    assert dump(report.backup_path) == dump(original)
    first = dump(path)
    again = migrations.migrate(path)
    assert again.changes == again.turns_migrated == again.conversations_created == 0
    assert again.backup_path is None and dump(path) == first
    assert not migrations.migration_needed(path)
    assert len(list(report.backup_path.parent.glob("*.sqlite"))) == 1
    assert original.read_bytes() == untouched
    repo = ConversationRepo(path)
    assert len(repo.list(UID)) == 4 and len(repo.list(OTHER)) == 1
    assert len(repo.set_history(UID, "A-1")) == 2
    assert all(t.question in ("질문 0", "질문 1") for t in repo.set_history(UID, "A-1"))
    for row in repo.list(UID):
        data = repo.get(UID, row["id"])
        assert data["version"] == 0 and len(data["turns"]) == row["turns"]
        if row["title"] == "A-1 Alpha":
            assert data["state"].subject.set_num == "A-1"
            assert data["state"].subject.source == "ui"
            assert len(data["turns"]) == 2
        elif row["title"] in ("탐색(이전)", "내 컬렉션(이전)"):
            assert data["state"] == DialogueState()
            assert not data["turns"][0].subjects
    assert {row["title"] for row in repo.list(UID)} == {"A-1 Alpha", "B-1", "탐색(이전)", "내 컬렉션(이전)"}
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT version FROM collections").fetchone() == (0,)
        for table in ("chat_requests", "collection_events"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)


def test_failure_rolls_back_ddl_and_rows(legacy_copy, monkeypatch):
    original, path = legacy_copy
    before = dump(path)
    real = migrations._import_legacy
    def fail(conn):
        real(conn)
        raise sqlite3.OperationalError("sensitive internals")
    monkeypatch.setattr(migrations, "_import_legacy", fail)
    with pytest.raises(migrations.MigrationError, match="이관에 실패") as exc:
        migrations.migrate(path)
    assert "sensitive" not in str(exc.value)
    assert dump(path) == before == dump(original)
    assert len(list((path.parent / "backup").glob("*.sqlite"))) == 1


def test_backup_failure_prevents_mutation(legacy_copy, monkeypatch):
    _, path = legacy_copy
    before = dump(path)
    def fail(_):
        raise OSError("private path")
    monkeypatch.setattr(migrations, "_backup", fail)
    with pytest.raises(migrations.MigrationError):
        migrations.migrate(path)
    assert dump(path) == before


def test_new_db_and_late_legacy_write(tmp_path):
    path = tmp_path / "new.sqlite"
    assert migrations.migration_needed(path) and not path.exists()
    first = migrations.migrate(path)
    assert first.changes and first.backup_path is None
    store = ConversationStore(path)
    store.append(UID, "A-1", "첫 질문", {})
    assert migrations.migration_needed(path)
    with pytest.raises(migrations.MigrationError):
        ConversationRepo(path)
    migrations.migrate(path)
    repo = ConversationRepo(path)
    cid = repo.list(UID)[0]["id"]
    store.append(UID, "A-1", "구 API 후속", {})
    report = migrations.migrate(path)
    assert report.conversations_created == 0 and report.turns_migrated == 1
    assert len(repo.get(UID, cid)["turns"]) == 2
    assert migrations.migrate(path).changes == 0


def test_backup_includes_wal_commits(tmp_path):
    path = tmp_path / "wal.sqlite"
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(LEGACY_SCHEMA)
        conn.execute("INSERT INTO collections VALUES (?)", (UID,))
        conn.commit()
        report = migrations.migrate(path)
        assert legacy_rows(report.backup_path)["collections"] == [(UID,)]


def test_future_version_is_not_downgraded(tmp_path):
    path = tmp_path / "future.sqlite"
    migrations.migrate(path)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("UPDATE schema_meta SET value='999'")
    before = dump(path)
    with pytest.raises(migrations.MigrationError):
        migrations.migrate(path)
    assert dump(path) == before


@pytest.mark.parametrize("broken", [False, True])
def test_status_reports_migration_without_running_it(legacy_copy, monkeypatch, broken):
    _, path = legacy_copy
    if broken:
        path.write_bytes(b"invalid database secret")
    before = path.read_bytes()
    cfg = {"paths": {"user_sqlite": str(path), "sqlite": str(path.parent / "lego.sqlite")}}
    monkeypatch.setattr(status, "_index_counts", lambda _: (1, 1))
    monkeypatch.setattr(status, "load_env", lambda: None)
    monkeypatch.delenv("GMS_KEY", raising=False)
    result = status.check_status(cfg)
    message_fragment = "이관 상태 확인에 실패" if broken else "이관이 필요"
    assert any(message_fragment in message for message in result.messages)
    assert result.migration_required is (not broken)
    assert path.read_bytes() == before and not (path.parent / "backup").exists()
    assert "secret" not in str(result)
