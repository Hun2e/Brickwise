"""공통 채팅 서비스의 실제 저장·컬렉션·문맥 계약을 임시 DB에서 검증한다."""

import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from rag_service.config import load_settings
from rag_service.planner.collections import CollectionStore
from rag_service.services import chat, collection_actions, orchestrator, users
from rag_service.services.conversations import ConversationRepo, StateConflict
from rag_service.services.dialogue_state import DialogueState, ShownList
from test_db_queries import conn, real_conn
from test_orchestrator_offline import deps, fake_ask, model


@pytest.fixture
def session(deps, tmp_path, monkeypatch):
    path = tmp_path / "reference.sqlite"
    deps.conn.commit()
    with closing(sqlite3.connect(path)) as copy:
        deps.conn.backup(copy)
    settings = deps.settings
    settings["paths"].update(sqlite=str(path), user_sqlite=str(tmp_path / "user.sqlite"))
    def configured(path=None):
        return load_settings(path) if path else settings
    for module in (chat, collection_actions, users):
        monkeypatch.setattr(module, "load_settings", configured)
    repo = ConversationRepo(tmp_path / "user.sqlite")
    deps.collection_store = CollectionStore(repo.path)
    deps.ask_fn = Mock(side_effect=fake_ask)
    deps.meta_store = object()
    actual_answer = orchestrator.answer
    calls = Mock(side_effect=lambda q, **kw: actual_answer(q, deps=deps, **kw))
    monkeypatch.setattr(orchestrator, "answer", calls)
    uid = users.new_user_id()
    cid = repo.create(uid)
    return uid, cid, repo, deps, calls


def send(session, question, request_id=None):
    uid, cid, *_ = session
    return chat.chat_turn_v2(uid, cid, question, request_id=request_id or uuid4().hex)


def test_seven_steps_real_db_and_latest_collection(session, real_conn):
    uid, cid, repo, deps, calls = session
    # 운영 DB는 read-only 연결에서 임시 사본으로 복제한 뒤 사본만 사용한다.
    with closing(sqlite3.connect(deps.settings["paths"]["sqlite"])) as copy:
        real_conn.backup(copy)
    with closing(sqlite3.connect(deps.settings["paths"]["sqlite"])) as copy:
        deps.conn = copy
        deps.model = model("fact", extra=[{"minifig": "Luke", "name_keywords": ["X-wing"]}])
        first = send(session, "루크가 들어 있는 엑스윙 찾아줘")
        assert first.response["items"] and not first.response["needs_input"]
        assert first.response["route"] == "filter"
        assert repo.get(uid, cid)["state"].last_list.items == tuple(i["set_num"] for i in first.response["items"])
        deps.model = model("filter", extra=[{"year_from": 2021, "year_to": 2021}])
        second = send(session, "그중 2021년에 나온 건?")
        assert second.response["set_nums"] == ["75301-1"]
        deps.model = model("fact")
        third = send(session, "부품이 몇 개야?")
        assert third.response["cards"] and third.response["route"] == "fact"
        fourth = send(session, "그거 하나 보유 목록에 등록해줘", "register")
        assert fourth.kind == "action" and fourth.response["action"]["qty_after"] == 1
        assert users.load_collection(uid)["sets"] == [("75301-1", 1)]
        deps.model = model("build")
        fifth = send(session, "내 부품으로 다른 건 뭐 만들 수 있어?")
        assert fifth.response["items"] and "75301-1" not in fifth.response["set_nums"]
        expected = deps.build_fn(copy, users.load_collection(uid), "exact", {
            "scope": deps.settings["planner"]["recommend_scope"],
            "top_n": deps.settings["planner"]["recommend_top_n"]})
        assert fifth.response["items"] == expected.items
        deps.model = model("narrative")
        sixth = send(session, "첫 번째 건 어떤 기체야?")
        selected = fifth.response["items"][0]["set_num"]
        assert deps.ask_fn.call_args.args[0] == selected
        assert sixth.response["state_summary"]["subject"] == selected
        deps.model = model("narrative")
        seventh = send(session, "색상은 무시하면 얼마나 부족해?")
        assert seventh.response["state_summary"]["mode"] == "ignore_color"
        assert "정확 일치 기준" in seventh.response["answer"]
        assert len(repo.get(uid, cid)["turns"]) == 7
        assert len(repo.set_history(uid, "75301-1")) == 3


def test_three_submissions_one_turn_one_change(session):
    uid, cid, repo, deps, calls = session
    rows = [send(session, "A-1 하나 더 추가해줘", "same") for _ in range(3)]
    assert rows[0] == rows[1] == rows[2]
    assert len(repo.get(uid, cid)["turns"]) == 1
    assert users.load_collection(uid)["sets"] == [("A-1", 1)]
    with sqlite3.connect(repo.path) as db:
        assert db.execute("SELECT COUNT(*) FROM collection_events").fetchone()[0] == 1
        assert db.execute("SELECT status, turn_id FROM chat_requests").fetchone() == ("done", rows[0].id)
    calls.assert_not_called()


@pytest.mark.parametrize("failure", [RuntimeError("secret Traceback prompt"), None])
def test_action_then_followup_failure_is_committed_once(session, failure):
    uid, cid, repo, deps, calls = session
    if failure:
        calls.side_effect = failure
    else:
        calls.side_effect = None
        calls.return_value = orchestrator.OrchestratorResponse(needs_input="API 키가 없습니다.")
    result = send(session, "A-1 하나 등록하고 어떤 기체인지 설명해줘", "partial")
    assert result.kind == "action" and "실패" in result.response["answer"]
    assert "secret" not in json.dumps(result.response)
    assert users.load_collection(uid)["sets"] == [("A-1", 1)]
    assert send(session, "A-1 하나 등록하고 어떤 기체인지 설명해줘", "partial") == result
    assert users.load_collection(uid)["version"] == 1
    assert calls.call_count == 1


def test_pending_target_quantity_confirmation_and_cancel(session):
    uid, cid, repo, deps, calls = session
    missing = send(session, "등록해줘")
    assert missing.kind == "clarify"
    assert repo.get(uid, cid)["state"].pending.kind == "target"
    quantity = send(session, "A-1")
    assert quantity.kind == "clarify" and repo.get(uid, cid)["state"].pending.kind == "quantity"
    action = send(session, "두 개")
    assert action.response["action"]["qty_after"] == 2
    confirm = send(session, "A-1 하나 있어")
    assert confirm.kind == "clarify" and repo.get(uid, cid)["state"].pending.kind == "confirm"
    assert users.load_collection(uid)["sets"] == [("A-1", 2)]
    send(session, "아니")
    assert users.load_collection(uid)["sets"] == [("A-1", 2)]
    send(session, "A-1 하나 있어")
    send(session, "응")
    assert users.load_collection(uid)["sets"] == [("A-1", 1)]
    assert repo.get(uid, cid)["state"].pending is None
    calls.assert_not_called()


def test_pending_candidates_resume_action(session):
    uid, cid, repo, deps, calls = session
    repo.save_turn_with_state(uid, cid, question="목록", response={}, kind="answer", subjects=[],
        state=DialogueState(last_list=ShownList("search", None, ("B-1", "A-1"))), expected_version=0, request_id=None)
    clarification = send(session, "그거 하나 등록해줘")
    assert [i["set_num"] for i in clarification.response["items"]] == ["B-1", "A-1"]
    assert chat.get_conversation(uid, cid)["state"].pending.request["intent"] == "own"
    selected = send(session, "첫 번째")
    assert selected.response["action"]["set_num"] == "B-1"
    assert repo.get(uid, cid)["state"].subject.set_num == "B-1"
    calls.assert_not_called()


def test_history_isolation_and_comparison_reaches_both_sets(session):
    uid, cid, repo, deps, calls = session
    for number in ("A-1", "B-1"):
        deps.model = model("narrative")
        send(session, number + " 어떤 기체야?")
        assert deps.ask_fn.call_args.kwargs["history"] == []
    deps.model = model("narrative")
    send(session, "B-1 어떤 기체야?")
    history = deps.ask_fn.call_args.kwargs["history"]
    assert len(history) == 1 and history[0]["question"].startswith("B-1")
    deps.model = model("compare")
    compare = send(session, "A-1 B-1 부품 수 비교")
    assert set(compare.subjects) == {("A-1", "compared"), ("B-1", "compared")}
    assert repo.set_history(uid, "A-1")[-1].id == repo.set_history(uid, "B-1")[-1].id == compare.id


@pytest.mark.parametrize("conflicts", [1, 2])
def test_state_conflict_retry_preserves_latest_unrelated_state(session, monkeypatch, conflicts):
    uid, cid, repo, deps, calls = session
    actual = ConversationRepo.save_turn_with_state
    attempts = []
    def conflicting(self, *args, **kwargs):
        attempts.append(kwargs["expected_version"])
        if len(attempts) <= conflicts:
            current, version = self.load_state(uid, cid)
            with sqlite3.connect(repo.path) as db:
                db.execute("UPDATE conversation_state SET state_json=?, version=version+1 WHERE conversation_id=?",
                           (replace(current, mode="ignore_color").to_json(), cid))
            raise StateConflict()
        return actual(self, *args, **kwargs)
    monkeypatch.setattr(ConversationRepo, "save_turn_with_state", conflicting)
    result = send(session, "A-1 하나 추가해줘", "conflict")
    assert attempts == [0, 1]
    assert users.load_collection(uid)["version"] == 1
    if conflicts == 1:
        assert result.id and result.response["state_summary"]["mode"] == "ignore_color"
    else:
        assert result.id == 0 and "상태 저장 충돌" in result.response["warnings"][-1]
        assert not repo.get(uid, cid)["turns"]
        monkeypatch.setattr(ConversationRepo, "save_turn_with_state", actual)
        retry = send(session, "A-1 하나 추가해줘", "conflict")
        assert retry.id and users.load_collection(uid)["version"] == 1


def test_ledger_recovery_and_user_scoped_ids(session):
    uid, cid, repo, deps, calls = session
    result = send(session, "A-1 하나 등록해줘", "recover")
    with sqlite3.connect(repo.path) as db:
        db.execute("UPDATE chat_requests SET status='running', turn_id=NULL")
    assert send(session, "A-1 하나 등록해줘", "recover") == result
    other = users.new_user_id()
    chat.chat_turn_v2(other, repo.create(other), "B-1 하나 등록해줘", request_id="recover")
    assert users.load_collection(other)["sets"] == [("B-1", 1)]
    with pytest.raises(chat.RequestConflict):
        chat.chat_turn_v2(uid, repo.create(uid), "A-1 하나 등록해줘", request_id="recover")


def test_missing_key_does_not_commit_resolved_subject(session, monkeypatch):
    from rag_service.llm import MissingApiKey
    monkeypatch.setattr(orchestrator, "get_chat_model", Mock(side_effect=MissingApiKey()))
    result = send(session, "A-1 부품 몇 개야?")
    assert "API 키" in result.response["needs_input"]
    assert result.response["state_summary"]["subject"] is None
    assert result.id


@pytest.mark.parametrize("question", ["A-1 더 설명해줘", "A-1 추가 설명해줘"])
def test_more_explanation_is_not_missing_collection_quantity(session, question):
    uid, cid, repo, deps, calls = session
    deps.model = model("narrative")
    result = send(session, question)
    assert result.kind == "answer" and "action" not in result.response
    assert users.load_collection(uid)["sets"] == []
    calls.assert_called_once()
