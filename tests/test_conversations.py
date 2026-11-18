import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from uuid import UUID

import pytest

from rag_service.planner.collections import CollectionStore
from rag_service.services.conversations import ConversationStore, history_for
from rag_service.services import users, catalog
from rag_service.services.users import new_user_id, valid_user_id, collection_id_for


def test_ids():
    uid = new_user_id()
    assert UUID(hex=uid).version == 4
    assert valid_user_id(uid) and collection_id_for(uid) == uid
    assert new_user_id() != uid


@pytest.mark.parametrize("uid", [None, "", "../user", "a" * 31, "a" * 33, "g" * 32, "a" * 32 + "\n"])
def test_bad_ids(uid):
    assert not valid_user_id(uid)
    with pytest.raises(ValueError):
        collection_id_for(uid)


def test_persistence_isolation_latest_order_and_delete(tmp_path):
    path = tmp_path / "user.sqlite"
    store = ConversationStore(path)
    uid, other = new_user_id(), new_user_id()
    for index in range(6):
        store.append(uid, "75192-1", str(index), {"answer": f"응답 {index}", "cards": [{"n": 1}]})
    store.append(uid, "explore", "탐색", {"answer": "탐색 결과"})
    store.append(uid, "collection", "컬렉션", {})
    store.append(uid, "75300-1", "다른 세트", {})
    store.append(other, "75192-1", "다른 사용자", {})
    restored = ConversationStore(path)
    turns = restored.turns(uid, "75192-1", limit=4)
    assert [turn.question for turn in turns] == ["2", "3", "4", "5"]
    assert turns[-1].response == {"answer": "응답 5", "cards": [{"n": 1}]}
    assert history_for(turns, 2) == [{"question": str(i), "answer": f"응답 {i}"} for i in (4, 5)]
    assert history_for(turns, 0) == []
    assert restored.turns(uid, "75192-1", limit=0) == []
    assert [t["thread"] for t in restored.threads(uid)] == ["75300-1", "collection", "explore", "75192-1"]
    assert restored.threads(uid)[-1]["turns"] == 6
    assert restored.clear(uid, "75192-1") == 6
    assert restored.clear(uid, "75192-1") == 0
    assert restored.turns(other, "75192-1")[0].question == "다른 사용자"
    assert len(restored.threads(uid)) == 3


def test_concurrent_append(tmp_path):
    store, uid = ConversationStore(tmp_path / "user.sqlite"), new_user_id()
    with ThreadPoolExecutor(max_workers=4) as pool:
        turns = list(pool.map(lambda i: store.append(uid, "explore", str(i), {}), range(20)))
    assert len({t.id for t in turns}) == 20
    assert len(store.turns(uid, "explore")) == 20


def test_additive_schema_preserves_existing_collection(tmp_path):
    path, uid = tmp_path / "user.sqlite", new_user_id()
    # 초기 컬렉션 스키마에서의 마이그레이션을 재현한다.
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.executescript("CREATE TABLE collections(id TEXT PRIMARY KEY);"
                           "CREATE TABLE collection_sets(collection_id TEXT, set_num TEXT, qty INTEGER, "
                           "PRIMARY KEY(collection_id, set_num));")
        conn.execute("INSERT INTO collections VALUES (?)", (uid,))
        conn.execute("INSERT INTO collection_sets VALUES (?, '75192-1', 2)", (uid,))
    ConversationStore(path)
    collection = CollectionStore(path)
    collection.ensure(uid)
    collection.add_adjustment(uid, "3001", 1, -1)
    collection.ensure(uid)
    assert collection.get(uid)["sets"] == [("75192-1", 2)]
    assert collection.get(uid)["adjustments"] == [("3001", 1, -1)]


def test_collection_services_no_eager_row_and_validation(tmp_path, monkeypatch):
    path, uid = tmp_path / "user.sqlite", new_user_id()
    monkeypatch.setattr(users, "user_db_path", lambda: path)
    monkeypatch.setattr(catalog, "catalog_options", lambda: [catalog.SetOption("A-1", "A", 2020, 1, "core", [], "")])
    assert users.load_collection(uid)["sets"] == []
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM collections").fetchone()[0] == 0
    users.save_collection(uid, [("A-1", 2)])
    users.save_collection(uid, [("A-1", 3)])
    assert users.load_collection(uid)["sets"] == [("A-1", 3)]
    assert users.load_collection(new_user_id())["sets"] == []
    for sets in [[("unknown-1", 2)], [("A-1", 0)], [("A-1", 1.5)], [("A-1", True)]]:
        with pytest.raises(ValueError):
            users.save_collection(uid, sets)
    assert users.load_collection(uid)["sets"] == [("A-1", 3)]
    users.save_collection(uid, [])
    assert users.load_collection(uid)["sets"] == []


def test_invalid_thread_and_limit(tmp_path):
    store, uid = ConversationStore(tmp_path / "user.sqlite"), new_user_id()
    with pytest.raises(ValueError):
        store.append(uid, "../../foo", "question", {})
    with pytest.raises(ValueError):
        store.turns(uid, "explore", -1)
