"""새 대화 REST/SSE 계약과 복원·격리·안전한 오류를 검증한다."""

import sqlite3
from dataclasses import asdict

from rag_service.services import chat, users
from rag_service.services.conversations import ConversationRepo, ConversationStore
from rag_service.services.dialogue_state import DialogueState, Pending, ShownList
from rag_service.services.migrations import migrate
from server import main
from test_server_support import api, ask, events, assert_safe


def test_rest_restore_pending_order_next_ordinal_and_subject_clear(api):
    state = DialogueState(last_list=ShownList("search", None, ("B-1", "A-1")),
        pending=Pending("target", ("B-1", "A-1"), {"intent": "fact", "question": "부품 수"}))
    api.store.save_turn_with_state(api.uid, api.cid, question="목록", response=asdict(main_turn()),
        kind="clarify", subjects=[("B-1", "listed"), ("A-1", "listed")], state=state,
        expected_version=1, request_id=None)
    restored = api.client.get(f"/api/conversations/{api.cid}", headers=api.headers).json()
    assert restored["state"]["last_list"]["items"] == ["B-1", "A-1"]
    assert restored["state"]["pending"]["kind"] == "target"
    assert restored["state_summary"]["last_list_size"] == 2
    assert api.client.get("/api/sets/B-1/history", headers=api.headers).json() == []
    result = events(ask(api, question="첫 번째 건"))[-1][1]
    ctx = api.responder.call_args.kwargs["context"]
    assert ctx.subject == "B-1" and ctx.resume["intent"] == "fact"
    # 대역 오케스트레이터도 실제처럼 문맥 delta를 반환한다.
    body = {"set_num": "B-1", "version": result["turn"]["state_summary"]["version"]}
    changed = api.client.patch(f"/api/conversations/{api.cid}/subject", headers=api.headers, json=body)
    assert changed.status_code == 200
    assert changed.json()["state"]["subject"]["set_num"] == "B-1"
    assert changed.json()["state"]["pending"] is None
    assert changed.json()["state"]["last_list"]["items"] == ["B-1", "A-1"]
    clear = api.client.patch(f"/api/conversations/{api.cid}/subject", headers=api.headers,
                            json={"set_num": None, "version": changed.json()["version"]})
    assert clear.json()["state"]["subject"] is None
    listed = api.client.get("/api/conversations", headers=api.headers).json()
    assert listed[0]["id"] == api.cid and listed[0]["turns"] == 2


def main_turn():
    from test_server_support import answer
    return answer()


def test_create_ownership_and_missing_conversation(api):
    listed = api.client.get("/api/conversations", headers=api.headers)
    assert listed.status_code == 200 and listed.json()[0]["subject"]["set_num"] == "A-1"
    created = api.client.post("/api/conversations", headers=api.headers)
    assert created.status_code == 200 and created.json()["id"] != api.cid
    other = {"X-User-Id": users.new_user_id()}
    for method, path, body in [
        ("GET", f"/api/conversations/{api.cid}", None),
        ("DELETE", f"/api/conversations/{api.cid}", None),
        ("PATCH", f"/api/conversations/{api.cid}/subject", {"set_num": None, "version": 1}),
        ("POST", "/api/chat", {"conversation_id": api.cid, "request_id": "x", "question": "질문"}),
    ]:
        response = api.client.request(method, path, headers=other, json=body)
        assert response.status_code == 404
        assert_safe(response)
    assert ask(api, conversation_id="missing").status_code == 404


def test_state_set_and_collection_conflicts_are_safe(api):
    path = f"/api/conversations/{api.cid}/subject"
    conflict = api.client.patch(path, headers=api.headers, json={"set_num": None, "version": 0})
    assert conflict.status_code == 409 and conflict.json()["code"] == "state_conflict"
    missing = api.client.patch(path, headers=api.headers, json={"set_num": "missing-1", "version": 1})
    assert missing.status_code == 404 and missing.json()["code"] == "set_not_found"
    assert ask(api, question="99999 부품 수").status_code == 200
    first = api.client.put("/api/collection", headers=api.headers, json={"sets": [["A-1", 1]], "version": 0})
    assert first.status_code == 200 and first.json()["version"] == 1
    stale = api.client.put("/api/collection", headers=api.headers, json={"sets": [], "version": 0})
    assert stale.status_code == 409 and stale.json()["code"] == "collection_conflict"
    assert users.load_collection(api.uid)["sets"] == [("A-1", 1)]
    for response in (conflict, missing, stale):
        assert_safe(response)


def test_sse_action_replay_and_request_conflict(api):
    results = [events(ask(api, question="A-1 하나 추가해줘", request_id="same"))[-1][1] for _ in range(3)]
    assert results[0] == results[1] == results[2]
    assert results[0]["action"]["qty_before"] == 0 and results[0]["action"]["qty_after"] == 1
    assert results[0]["turn"]["action"] == results[0]["action"]
    assert users.load_collection(api.uid)["version"] == 1
    assert len(api.store.get(api.uid, api.cid)["turns"]) == 1
    restored = api.client.get(f"/api/conversations/{api.cid}", headers=api.headers).json()
    assert results[0]["state"] == restored["state"]
    conflict = ask(api, conversation_id=api.store.create(api.uid), request_id="same")
    assert conflict.status_code == 409 and conflict.json()["code"] == "request_conflict"


def test_comparison_is_in_both_set_history_endpoints(api):
    result = events(ask(api, question="A-1 B-1 부품 수 비교"))[-1][1]["turn"]
    for number in ("A-1", "B-1"):
        rows = api.client.get(f"/api/sets/{number}/history", headers=api.headers).json()
        assert rows == [result]
        assert rows[0]["comparison_label"] == "비교: A-1·B-1"


def test_durable_busy_before_sse_without_in_process_lock(api):
    job = chat.reserve_chat_request(api.uid, api.cid, "질문", "busy")
    result = ask(api, request_id="busy")
    assert result.status_code == 409 and result.headers["content-type"] == "application/json"
    chat.fail_chat_request(job)
    assert ask(api, request_id="busy").status_code == 200


def test_legacy_migration_copy_reads_only_and_no_automatic_migration(api, tmp_path):
    legacy = tmp_path / "legacy.sqlite"
    old = ConversationStore(legacy)
    old.append(api.uid, "A-1", "이전 질문", asdict(main_turn()))
    # 운영 DB 대신 테스트용 구 DB의 사본을 이관한다.
    copy = tmp_path / "legacy-copy.sqlite"
    with sqlite3.connect(legacy) as source, sqlite3.connect(copy) as dest:
        source.backup(dest)
    migrate(copy)
    api.monkeypatch.setattr(chat, "conversation_repo", lambda: ConversationRepo(copy))
    rows = api.client.get("/api/threads", headers=api.headers).json()
    assert rows[0]["thread"] == "A-1" and rows[0]["turns"] == 1
    turns = api.client.get("/api/threads/A-1/turns", headers=api.headers).json()
    assert turns[0]["question"] == "이전 질문"
    for method, path in [("DELETE", "/api/threads/A-1"), ("POST", "/api/threads"),
                         ("PUT", "/api/threads/A-1/turns"), ("DELETE", "/api/threads/A-1/turns")]:
        response = api.client.request(method, path, headers=api.headers, json={})
        assert response.status_code == 410 and response.json()["code"] == "legacy_read_only"
    before = legacy.read_bytes()
    api.monkeypatch.setattr(chat, "conversation_repo", lambda: ConversationRepo(legacy))
    response = api.client.get("/api/conversations", headers=api.headers)
    assert response.status_code == 503 and response.json()["code"] == "migration_required"
    assert legacy.read_bytes() == before
    assert_safe(response)
