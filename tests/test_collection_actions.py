"""임시 참조 DB·사용자 DB로 의도 파싱과 변경의 원자성·멱등을 검증한다."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from threading import Barrier

import pytest

from rag_service.config import load_settings, project_root
from rag_service.planner import collections
from rag_service.planner.collections import CollectionConflict, CollectionStore
from rag_service.services import collection_actions as actions, users
from rag_service.services.migrations import migrate

UID = "a" * 32
OTHER = "b" * 32


@pytest.mark.parametrize("question,kind,op,qty,statement", [
    ("75301 하나 등록해줘", "own", "set", 1, False),
    ("75301 두 개 있어", "own", "set", 2, True),
    ("그거 하나 더 추가해줘", "own", "add", 1, False),
    ("실제로는 두 개야", "own", "set", 2, False),
    ("3개로 바꿔줘", "own", "set", 3, False),
    ("그거 보유 목록에서 빼줘", "own", "remove", None, False),
    ("삭제해줘", "own", "remove", None, False),
    ("그거 가지고 싶어", "wish", "none", None, False),
    ("사고 싶어", "wish", "none", None, False),
    ("그거 하나 있어", "own", "set", 1, True),
    ("75301하나등록해줘", "own", "set", 1, False),
    ("75301  한  개  등록해주세요", "own", "set", 1, False),
    ("75301-1 2개 등록해줄래?", "own", "set", 2, False),
    ("그거하나더추가해줘", "own", "add", 1, False),
    ("75301 두개더", "own", "add", 2, False),
    ("그거2개등록해줘", "own", "set", 2, False),
    ("그거세개를가지고있어", "own", "set", 3, True),
    ("그거 둘 있어", "own", "set", 2, True),
    ("세 개 가지고 있어", "own", "set", 3, True),
    ("셋 있어요", "own", "set", 3, True),
    ("네 개 갖고 있어", "own", "set", 4, True),
    ("넷 있어", "own", "set", 4, True),
    ("다섯 개 등록", "own", "set", 5, False),
    ("여섯개 추가해줘", "own", "add", 6, False),
    ("일곱개로 수정", "own", "set", 7, False),
    ("여덟 개로 변경해줘", "own", "set", 8, False),
    ("아홉개 보유 중", "own", "set", 9, True),
    ("열개있어", "own", "set", 10, True),
    ("99 개 등록해줘", "own", "set", 99, False),
    ("실제로는두개야", "own", "set", 2, False),
    ("사실은 하나 있어", "own", "set", 1, False),
    ("그거 목록에서 제거해줘", "own", "remove", None, False),
    ("보유 목록에서 빼 주세요", "own", "remove", None, False),
    ("두개로 바꿔 주세요", "own", "set", 2, False),
    ("그거 1개 더 넣어줘", "own", "add", 1, False),
    ("75301 하나 구매하고 싶어", "wish", "none", None, False),
    ("두 개 갖고 싶어", "wish", "none", None, False),
    ("그거 살까", "wish", "none", None, False),
    ("하나 더 사고 싶어", "wish", "none", None, False),
])
def test_parse_variants(question, kind, op, qty, statement):
    assert actions.parse_intent(question) == actions.CollectionIntent(kind, op, qty, statement)


@pytest.mark.parametrize("question", [
    "그거 없어", "하나도 없어", "두 개 안 가지고 있어", "등록하지 마", "하나 등록하지 말아줘",
    "그거 삭제하지 말아줘", "한개 추가 안 해", "두 개 가지고 있지 않아", "두 개가 아니야",
    "이 세트 설명해줘", "", "75301 등록", "등록해줘", "하나 있어?", "몇 개 있어?", "두 개 있나요",
    "0개 등록", "100개 추가", "1.5개 등록", "-1개 추가", "하나 또는 두개 등록", "2개 3개 등록",
    "하나 등록하지 마", "한개 추가하지 마세요", "열한개 등록", "스물한개 있어", "1,001개 등록",
])
def test_non_mutating_and_ambiguous_intents(question):
    intent = actions.parse_intent(question)
    assert intent is None or intent.op == "none"


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    settings = load_settings()
    reference, user_db = tmp_path / "lego.sqlite", tmp_path / "user.sqlite"
    settings["paths"].update(sqlite=str(reference), user_sqlite=str(user_db))
    with closing(sqlite3.connect(reference)) as conn, conn:
        conn.executescript((project_root() / "src/rag_service/db/schema.sql").read_text(encoding="utf-8"))
        conn.executemany("INSERT INTO sets VALUES (?, ?, 2020, 1, 100, '')", [
            ("75301-1", "Luke Skywalker's X-Wing Fighter"), ("75300-1", "TIE Fighter")])
        conn.executemany("INSERT INTO set_scope VALUES (?, 'core')", [("75301-1",), ("75300-1",)])
    original_loader = actions.load_settings
    monkeypatch.setattr(actions, "load_settings", lambda path=None: settings if path is None else original_loader(path))
    monkeypatch.setattr(users, "load_settings", lambda: settings)
    monkeypatch.setattr(collections, "load_settings", lambda: settings)
    from rag_service.services import catalog
    monkeypatch.setattr(catalog, "load_settings", lambda: settings)
    store = CollectionStore(user_db)
    return store, settings


def change(request_id="request", **kwargs):
    return actions.apply(UID, **{ "set_num": "75301", "op": "add", "qty": 1,
                                 "request_id": request_id, **kwargs})


def snapshot(store):
    with closing(sqlite3.connect(store.path)) as conn:
        return {table: conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
                for table in ("collections", "collection_sets", "collection_events", "collection_adjustments")}


def test_replay_three_times_and_after_later_changes(isolated):
    store, _ = isolated
    first = change()
    before = snapshot(store)
    for _ in range(2):
        replay = change()
        assert replay == replace(first, replayed=True)
        assert snapshot(store) == before
    change("later")
    users.save_collection(UID, [("75300-1", 3)], expected_version=2)
    assert change() == replace(first, replayed=True)
    assert users.load_collection(UID)["version"] == 3


def test_concurrent_twenty_additions(isolated):
    store, _ = isolated
    change("initial", op="set", qty=2)
    barrier = Barrier(20)
    def add(index):
        barrier.wait()
        return change(f"parallel-{index}")
    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(add, range(20)))
    assert store.get(UID)["sets"] == [("75301-1", 22)]
    assert store.get(UID)["version"] == 21
    assert {r.version for r in results} == set(range(2, 22))
    assert len(snapshot(store)["collection_events"]) == 21


def test_concurrent_same_request(isolated):
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: change(), range(8)))
    assert sum(not r.replayed for r in results) == 1
    assert isolated[0].get(UID)["sets"] == [("75301-1", 1)]
    assert isolated[0].get(UID)["version"] == 1


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("replacement", [False, True])
def test_rollback_after_event_and_version_write(isolated, monkeypatch, existing, replacement):
    store, _ = isolated
    if existing:
        change("initial")
    before = snapshot(store)
    original = CollectionStore._record_change
    def fail(*args):
        original(*args)
        raise RuntimeError("injected failure")
    monkeypatch.setattr(CollectionStore, "_record_change", staticmethod(fail))
    with pytest.raises(RuntimeError, match="injected failure"):
        if replacement:
            users.save_collection(UID, [("75300-1", 2)])
        else:
            change()
    assert snapshot(store) == before


@pytest.mark.parametrize("qty", [0, 100, 1.5, True, -1, "1", None])
@pytest.mark.parametrize("op", ["set", "add"])
def test_invalid_qty_does_not_mutate(isolated, qty, op):
    before = snapshot(isolated[0])
    with pytest.raises(ValueError):
        change(op=op, qty=qty)
    assert snapshot(isolated[0]) == before


def test_invalid_target_and_add_overflow(isolated):
    store, _ = isolated
    before = snapshot(store)
    with pytest.raises(actions.SetNotFound):
        change(set_num="unknown")
    assert snapshot(store) == before
    change("initial", op="set", qty=99)
    before = snapshot(store)
    with pytest.raises(ValueError):
        change()
    assert snapshot(store) == before


def test_confirmation_and_removal(isolated):
    store, _ = isolated
    change("initial", op="set", qty=2)
    before = snapshot(store)
    pending = change(op="set", statement=True)
    assert pending.needs_confirm and not pending.applied and not pending.replayed
    assert (pending.qty_before, pending.qty_after, pending.version) == (2, 1, 1)
    assert actions.describe(pending) == "지금 2개로 기록돼 있어요. 1개로 줄일까요?"
    assert snapshot(store) == before
    confirmed = change(op="set", statement=True, confirm=True)
    assert confirmed.applied and not confirmed.needs_confirm and confirmed.version == 2
    assert change(op="set", statement=True, confirm=True) == replace(confirmed, replayed=True)
    removed = change("remove", op="remove", qty=None)
    assert (removed.qty_before, removed.qty_after, removed.version) == (1, 0, 3)
    assert store.get(UID)["sets"] == []
    assert "삭제했어요" in actions.describe(removed)


def test_statement_increase_and_explicit_reduction(isolated):
    change("initial", op="set", qty=2, statement=True)
    assert change("increase", op="set", qty=3, statement=True).applied
    assert change("explicit", op="set", qty=1).applied
    assert isolated[0].get(UID)["sets"] == [("75301-1", 1)]


def test_editor_conflict_replace_event_and_compatibility(isolated):
    store, _ = isolated
    assert users.load_collection(UID)["version"] == 0
    users.save_collection(UID, [("75301-1", 2)], expected_version=0)
    store.add_adjustment(UID, "part", 1, -1)
    before = snapshot(store)
    with pytest.raises(CollectionConflict):
        users.save_collection(UID, [], expected_version=0)
    assert snapshot(store) == before
    users.save_collection(UID, [("75300-1", 1)], expected_version=1)
    users.save_collection(UID, [])
    collection = store.get(UID)
    assert collection["sets"] == [] and collection["adjustments"] == [("part", 1, -1)]
    assert collection["version"] == 3
    with closing(sqlite3.connect(store.path)) as conn:
        assert conn.execute("SELECT op, qty_before, qty_after FROM collection_events").fetchall() == [
            ("replace", 0, 2), ("replace", 2, 1), ("replace", 1, 0)]


def test_editor_conflict_for_new_user_and_duplicate_quantity_cap(isolated):
    before = snapshot(isolated[0])
    with pytest.raises(CollectionConflict):
        users.save_collection(UID, [], expected_version=1)
    for sets in [[("75301-1", 100)], [("75301-1", 50), ("75301-1", 50)]]:
        with pytest.raises(ValueError):
            users.save_collection(UID, sets)
    assert snapshot(isolated[0]) == before


def test_cross_user_request_collision_never_replays_another_user(isolated):
    change()
    before = snapshot(isolated[0])
    with pytest.raises(CollectionConflict):
        actions.apply(OTHER, set_num="75301", op="add", qty=1, request_id="request")
    assert snapshot(isolated[0]) == before
    other = actions.apply(OTHER, set_num="75301", op="add", qty=1, request_id="other")
    assert other.qty_before == 0 and other.version == 1


def test_custom_max_and_templates(isolated):
    _, settings = isolated
    settings["collection"]["max_qty"] = 2
    assert actions.parse_intent("3개 등록", settings) is None
    with pytest.raises(ValueError):
        change(qty=3)
    result = change(op="set")
    assert actions.describe(result) == "75301-1 Luke Skywalker's X-Wing Fighter: 보유 0개 → 1개로 등록했어요."
    assert "이미 처리한 요청" in actions.describe(change(op="set"))


def test_create_after_migration_and_legacy_read(tmp_path):
    path = tmp_path / "legacy.sqlite"
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("CREATE TABLE collections(id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO collections VALUES (?)", (UID,))
    legacy = CollectionStore(path)
    assert legacy.get(UID)["version"] == 0
    before = snapshot(legacy)
    assert not (tmp_path / "backup").exists()
    legacy.set_sets(UID, [("75301-1", 1)])
    assert legacy.get(UID)["version"] == 1
    backups = list((tmp_path / "backup").glob("*.sqlite"))
    assert len(backups) == 1
    with closing(sqlite3.connect(backups[0])) as conn:
        assert conn.execute("SELECT * FROM collections").fetchall() == before["collections"]
        assert conn.execute("SELECT * FROM collection_sets").fetchall() == before["collection_sets"]
    migrate(path)
    migrated = CollectionStore(path)
    cid = migrated.create()
    assert migrated.get(cid) == {"id": cid, "sets": [], "adjustments": [], "version": 0}
    migrated.set_sets(cid, [("75301-1", 1)])
    assert migrated.get(cid)["version"] == 1
