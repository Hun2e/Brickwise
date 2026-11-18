import sqlite3
from contextlib import closing

import pytest

from rag_service.config import load_settings
from rag_service.services import status


@pytest.fixture
def settings(tmp_path, monkeypatch):
    cfg = load_settings()
    cfg["paths"].update(sqlite=str(tmp_path / "lego.sqlite"), user_sqlite=str(tmp_path / "user.sqlite"),
                         chroma_dir=str(tmp_path / "chroma"))
    with closing(sqlite3.connect(cfg["paths"]["sqlite"])) as conn, conn:
        conn.executescript("CREATE TABLE sets(set_num TEXT); CREATE TABLE set_scope(set_num TEXT);"
                           "INSERT INTO sets VALUES ('A-1'); INSERT INTO set_scope VALUES ('A-1');")
    monkeypatch.setattr(status, "load_env", lambda: None)
    monkeypatch.setenv("GMS_KEY", "fake-key-must-not-appear")
    return cfg


def test_missing_index_no_creation(settings):
    result = status.check_status(settings)
    assert result.db_ok and result.user_db_ok and result.api_key_set
    assert not result.can_chat and result.meta_chunks == result.doc_chunks == 0
    assert any("항목 색인이 0건" in m for m in result.messages)
    assert "fake-key" not in str(result)
    with closing(sqlite3.connect(settings["paths"]["user_sqlite"])) as conn:
        assert conn.execute("SELECT COUNT(*) FROM collections").fetchone()[0] == 0


def test_real_chroma_count_without_embeddings(settings):
    import chromadb
    from chromadb.config import Settings

    client = chromadb.PersistentClient(settings["paths"]["chroma_dir"],
                                      settings=Settings(anonymized_telemetry=False, _env_file=None))
    collection = client.create_collection(settings["collections"]["items"], embedding_function=None)
    collection.add(ids=["A-1"], embeddings=[[1.0, 0.0]], documents=["A"])
    result = status.check_status(settings)
    assert result.can_chat and result.meta_chunks == 1 and result.doc_chunks == 0
    assert len(client.list_collections()) == 1


@pytest.mark.parametrize("issue", ["db", "key", "write", "index", "empty_db"])
def test_failures_safe_and_block_chat(settings, monkeypatch, issue):
    monkeypatch.setattr(status, "_index_counts", lambda _: (1, 1))
    def fail(_):
        raise OSError("secret-token traceback system prompt")
    if issue == "db":
        settings["paths"]["sqlite"] += ".missing"
    elif issue == "key":
        monkeypatch.delenv("GMS_KEY")
    elif issue == "write":
        monkeypatch.setattr(status, "_user_db_writable", fail)
    elif issue == "index":
        monkeypatch.setattr(status, "_index_counts", fail)
    else:
        with closing(sqlite3.connect(settings["paths"]["sqlite"])) as conn, conn:
            conn.execute("DELETE FROM sets")
    result = status.check_status(settings)
    assert not result.can_chat and result.messages
    assert all(word not in str(result) for word in ("secret-token", "traceback", "system prompt", "fake-key"))


def test_ready(settings, monkeypatch):
    monkeypatch.setattr(status, "_index_counts", lambda _: (378, 50))
    result = status.check_status(settings)
    assert result.can_chat and result.messages == [] and result.set_count == 1


@pytest.mark.parametrize("indexed", [False, True])
def test_first_status_check_initializes_current_conversation_schema(settings, monkeypatch, indexed):
    from rag_service.services.conversations import ConversationRepo
    from rag_service.services.migrations import migration_needed
    from rag_service.services.users import new_user_id

    monkeypatch.setattr(status, "_index_counts", lambda _: (1, 1) if indexed else (0, 0))
    # 첫 화면의 상태 확인이 대화 생성보다 먼저 실행되어도 이관 안내로 막히지 않아야 한다.
    first = status.check_status(settings)
    second = status.check_status(settings)
    assert first.user_db_ok and second.user_db_ok
    assert not first.migration_required and not second.migration_required
    path = settings["paths"]["user_sqlite"]
    assert not migration_needed(path)
    assert ConversationRepo(path).create(new_user_id())
