import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from threading import Barrier

import pytest

from rag_service.services.conversations import ConversationRepo, StateConflict
from rag_service.services.dialogue_state import (
    DialogueState, Pending, ShownList, Subject, cleared, with_list, with_pending, with_subject,
)

UID, OTHER = "a" * 32, "b" * 32
SUBJECT = Subject("A-1", "explicit", 1)
SHOWN = ShownList("search", 2, ("B-1", "A-1"), spec={"year_min": 2021})
PENDING = Pending("confirm", ("A-1",), {"intent": "collection", "question": "하나 있어", "qty": 1})
STATE = DialogueState(SUBJECT, SHOWN, ("A-1", "B-1"), "ignore_color", PENDING)


@pytest.mark.parametrize("state", [DialogueState(), STATE, replace(STATE, last_list=ShownList(
    "recommend", 3, ("B-1", "A-1"), rec={"mode": "exact", "top_n": 2, "scope": "core"}))])
def test_state_round_trip(state):
    assert DialogueState.from_json(state.to_json()) == state
    assert "하나 있어" in STATE.to_json()


@pytest.mark.parametrize("event, expected", [
    (lambda s: DialogueState(), DialogueState()),
    (lambda s: with_subject(s, Subject("B-1", "card", 3)),
     DialogueState(Subject("B-1", "card", 3), SHOWN, (), "ignore_color", None)),
    (cleared, DialogueState(None, SHOWN, (), "ignore_color", None)),
    (lambda s: with_list(s, ShownList("search", 3, ("C-1",))),
     DialogueState(SUBJECT, ShownList("search", 3, ("C-1",)), STATE.compare, "ignore_color", None)),
    (lambda s: with_list(s, ShownList("recommend", 3, ("C-1",), rec={"mode": "exact", "top_n": 1, "scope": "core"})),
     DialogueState(SUBJECT, ShownList("recommend", 3, ("C-1",), rec={"mode": "exact", "top_n": 1, "scope": "core"}),
                   STATE.compare, "exact", None)),
    (lambda s: with_pending(s, Pending("quantity", ("B-1",), {"intent": "add", "question": "등록"})),
     replace(STATE, pending=Pending("quantity", ("B-1",), {"intent": "add", "question": "등록"}))),
    (lambda s: with_pending(s, PENDING), STATE),
    (lambda s: with_pending(s, Pending("target", ("B-1", "C-1"), {"intent": "fact", "question": "몇 개"})),
     replace(STATE, last_list=ShownList("candidates", None, ("B-1", "C-1")),
             pending=Pending("target", ("B-1", "C-1"), {"intent": "fact", "question": "몇 개"}))),
    (lambda s: with_pending(s, Pending("target", (), {"intent": "fact", "question": "몇 개"})),
     replace(STATE, pending=Pending("target", (), {"intent": "fact", "question": "몇 개"}))),
])
def test_reset_table_every_column(event, expected):
    original = STATE.to_json()
    assert event(STATE) == expected
    assert STATE.to_json() == original


@pytest.mark.parametrize("raw", ["", "{", "[]", "null", "{}", '{"schema_version":0}',
                                  '{"schema_version":99}', '{"private":"do not log"}'])
def test_corrupt_and_old_state_is_empty_with_safe_warning(raw, caplog):
    assert DialogueState.from_json(raw) == DialogueState()
    assert "빈 상태" in caplog.text and "do not log" not in caplog.text


@pytest.mark.parametrize("key, value", [
    ("subject", {"set_num": "../bad", "source": "ui", "turn_id": None}),
    ("subject", {"set_num": "A-1", "source": "guess", "turn_id": None}),
    ("subject", {"set_num": "A-1", "source": "ui", "turn_id": True}),
    ("last_list", {"kind": "search", "turn_id": None, "items": "A-1"}),
    ("last_list", {"kind": "bad", "turn_id": None, "items": []}),
    ("compare", "A-1"), ("mode", "bad"), ("pending", {"kind": "target", "options": [], "request": {}}),
    ("schema_version", True), ("unknown", 1),
])
def test_invalid_nested_state_rejected(key, value, caplog):
    data = json.loads(STATE.to_json())
    data[key] = value
    assert DialogueState.from_json(json.dumps(data)) == DialogueState()
    assert caplog.records


def test_none_and_mutated_mapping():
    assert DialogueState.from_json(None) == DialogueState()
    shown = ShownList("search", None, (), spec={})
    shown.spec["invalid"] = float("nan")
    with pytest.raises(ValueError):
        DialogueState(last_list=shown).to_json()
    candidates = ShownList("candidates", 7, ("B-1", "A-1"))
    state = replace(STATE, last_list=candidates)
    pending = Pending("target", candidates.items, {"intent": "fact", "question": "어느 세트"})
    assert with_pending(state, pending).last_list == candidates


@pytest.fixture
def repo(tmp_path):
    return ConversationRepo(tmp_path / "user.sqlite")


def save(repo, cid, *, uid=UID, number="A-1", subjects=None, version=None, request=None):
    state, current = repo.load_state(uid, cid)
    state = with_subject(state, Subject(number, "explicit", None))
    return repo.save_turn_with_state(uid, cid, question=f"{number} 질문", response={"answer": "답", "cards": [{"n": 1}]},
                                    kind="answer", subjects=subjects if subjects is not None else [(number, "primary")],
                                    state=state, expected_version=current if version is None else version, request_id=request)


def test_timeline_set_history_and_context_separation(repo):
    cid, second, other = repo.create(UID), repo.create(UID), repo.create(OTHER)
    a1 = save(repo, cid)
    b = save(repo, cid, number="B-1")
    compared = save(repo, cid, subjects=[("A-1", "compared"), ("B-1", "compared")])
    listed = save(repo, cid, subjects=[("A-1", "listed"), ("B-1", "listed")])
    a2 = save(repo, cid)
    another = save(repo, second)
    save(repo, other, uid=OTHER)
    assert [t.id for t in repo.get(UID, cid)["turns"]] == [a1.id, b.id, compared.id, listed.id, a2.id]
    assert [t.id for t in repo.set_history(UID, "A-1")] == [a1.id, compared.id, a2.id, another.id]
    assert [t.id for t in repo.set_history(UID, "B-1")] == [b.id, compared.id]
    assert compared.comparison_label == "비교: A-1·B-1"
    assert repo.set_history(UID, "B-1")[-1].comparison_label == compared.comparison_label
    assert [t.id for t in repo.context_turns(UID, cid, "A-1", 10)] == [a1.id, a2.id]
    assert [t.id for t in repo.context_turns(UID, cid, "A-1", 1)] == [a2.id]
    assert [t.id for t in repo.context_turns(UID, cid, "B-1", 10)] == [b.id]
    assert repo.context_turns(UID, cid, None, 10) == repo.context_turns(UID, cid, "A-1", 0) == []
    restored = ConversationRepo(repo.path).get(UID, cid)
    assert restored["version"] == 5 and restored["state"].subject.set_num == "A-1"
    assert restored["turns"][0].response["cards"] == [{"n": 1}]


def test_conflict_unique_request_and_sql_failure_are_atomic(repo):
    cid = repo.create(UID)
    save(repo, cid, request="request-1")
    before = repo.get(UID, cid)
    with pytest.raises(StateConflict):
        save(repo, cid, version=0)
    assert repo.get(UID, cid) == before
    with pytest.raises(sqlite3.IntegrityError):
        save(repo, cid, request="request-1")
    assert repo.get(UID, cid) == before
    with closing(sqlite3.connect(repo.path)) as conn, conn:
        conn.execute("CREATE TRIGGER fail_subject BEFORE INSERT ON turn_subjects "
                     "BEGIN SELECT RAISE(ABORT, 'injected failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        save(repo, cid)
    assert repo.get(UID, cid) == before
    assert len(repo.set_history(UID, "A-1")) == 1


def test_twenty_concurrent_saves_versions_and_no_lost_updates(repo):
    cid = repo.create(UID)
    start = Barrier(20)
    def worker(i):
        local = ConversationRepo(repo.path)
        start.wait()
        for _ in range(100):
            _, version = local.load_state(UID, cid)
            try:
                return save(local, cid, version=version, request=f"request-{i}")
            except StateConflict:
                continue
        pytest.fail("동시 저장 재시도 한도 초과")
    with ThreadPoolExecutor(max_workers=20) as pool:
        turns = list(pool.map(worker, range(20)))
    assert sorted(t.state_version for t in turns) == list(range(1, 21))
    saved = repo.get(UID, cid)
    assert saved["version"] == 20 and len(saved["turns"]) == 20
    assert [t.state_version for t in saved["turns"]] == list(range(1, 21))


def test_same_version_concurrent_only_one_winner(repo):
    cid = repo.create(UID)
    barrier = Barrier(2)
    def worker(_):
        barrier.wait()
        try:
            save(repo, cid, version=0)
            return True
        except StateConflict:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(worker, range(2))) == [False, True]
    assert repo.get(UID, cid)["version"] == 1


def test_ownership_and_delete_cascades(repo):
    cid, other = repo.create(UID, "내 대화"), repo.create(OTHER)
    turn = save(repo, cid)
    save(repo, other, uid=OTHER)
    for operation in (repo.get, repo.load_state, repo.delete):
        with pytest.raises(KeyError):
            operation(OTHER, cid)
    with pytest.raises(KeyError):
        save(repo, cid, uid=OTHER)
    with pytest.raises(KeyError):
        repo.context_turns(OTHER, cid, "A-1", 1)
    assert repo.list(UID)[0]["title"] == "내 대화"
    assert repo.list(UID)[0]["turns"] == 1
    with closing(sqlite3.connect(repo.path)) as conn, conn:
        conn.execute("INSERT INTO chat_requests VALUES (?, 'request', ?, 'done', ?, 'now')", (UID, cid, turn.id))
    assert repo.delete(UID, cid) == 1
    assert repo.list(UID) == repo.set_history(UID, "A-1") == []
    assert len(repo.get(OTHER, other)["turns"]) == 1
    with closing(sqlite3.connect(repo.path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM turn_subjects WHERE turn_id=?", (turn.id,)).fetchone() == (0,)
        assert conn.execute("SELECT COUNT(*) FROM conversation_state WHERE conversation_id=?", (cid,)).fetchone() == (0,)
        assert conn.execute("SELECT COUNT(*) FROM chat_requests").fetchone() == (0,)


@pytest.mark.parametrize("subjects", [[("A-1", "primary"), ("B-1", "primary")],
                                      [("B-1", "primary"), ("A-1", "compared")], [("A-1", "bad")]])
def test_invalid_links_do_not_save(repo, subjects):
    cid = repo.create(UID)
    with pytest.raises(ValueError):
        save(repo, cid, subjects=subjects)
    assert repo.get(UID, cid)["version"] == 0 and repo.get(UID, cid)["turns"] == []


def test_general_thread_and_duplicate_links(repo):
    cid = repo.create(UID)
    repo.save_turn_with_state(UID, cid, question="검색", response={}, kind="search", subjects=[],
                              state=DialogueState(), expected_version=0, request_id=None)
    save(repo, cid, subjects=[("A-1", "primary"), ("A-1", "primary")])
    with closing(sqlite3.connect(repo.path)) as conn:
        assert conn.execute("SELECT thread FROM conversation_turns ORDER BY id").fetchall() == [("general",), ("A-1",)]
    assert len(repo.set_history(UID, "A-1")) == 1


def test_legacy_clear_cleans_links_after_migration(repo):
    from rag_service.services.conversations import ConversationStore

    cid = repo.create(UID)
    turn = save(repo, cid)
    assert ConversationStore(repo.path).clear(UID, "A-1") == 1
    assert repo.get(UID, cid)["turns"] == []
    with closing(sqlite3.connect(repo.path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM turn_subjects WHERE turn_id=?", (turn.id,)).fetchone() == (0,)


def test_stored_invalid_state_keeps_optimistic_version(repo, caplog):
    cid = repo.create(UID)
    save(repo, cid)
    with closing(sqlite3.connect(repo.path)) as conn, conn:
        conn.execute("UPDATE conversation_state SET state_json='broken'")
    assert repo.load_state(UID, cid) == (DialogueState(), 1)
    assert "빈 상태" in caplog.text
    assert save(repo, cid).state_version == 2


@pytest.mark.parametrize("invalid", [-1, True, 1.5])
def test_invalid_limits_and_versions(repo, invalid):
    cid = repo.create(UID)
    with pytest.raises(ValueError):
        save(repo, cid, version=invalid)
    with pytest.raises(ValueError):
        repo.context_turns(UID, cid, "A-1", invalid)
    assert repo.get(UID, cid)["version"] == 0
