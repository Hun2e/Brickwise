import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import Mock

import pytest

from rag_service.services import orchestrator
from server import main
from server.deps import SERVER_SETTINGS
from test_server_support import SECRET, answer, api, ask, assert_safe, events


def test_sse_stages_final_result_storage_and_projection(api):
    result = ask(api)
    assert result.status_code == 200
    assert result.headers["content-type"].startswith("text/event-stream")
    assert result.headers["x-accel-buffering"] == "no"
    assert result.headers["cache-control"] == "no-cache"
    rows = events(result)
    assert [kind for kind, _ in rows] == ["stage"] * 4 + ["result"]
    assert [(row["stage"], row["index"]) for _, row in rows[:-1]] == [("분류", 0), ("문서 수집", 2), ("생성", 4), ("검증", 5)]
    turn = rows[-1][1]["turn"]
    assert turn["thread"] == "A-1" and turn["saved"] and turn["id"] > 0
    assert [card["n"] for card in turn["answer"]["cards"]] == [7]
    assert not {"usage", "verification", "route_source"} & turn["answer"].keys()
    stored = api.store.get(api.uid, api.cid)["turns"][0]
    assert stored.response["usage"]["private"] == SECRET
    assert len(stored.response["cards"]) == 2
    history = api.client.get(f"/api/conversations/{api.cid}", headers=api.headers)
    assert history.json()["turns"] == [turn]
    assert_safe(result)
    assert_safe(history)


@pytest.mark.parametrize("changes", [
    {"question": ""}, {"question": " \n\t"}, {"question": "x" * (SERVER_SETTINGS["max_question_chars"] + 1)},
    {"question": None}, {"question": 3}, {"conversation_id": ""}, {"request_id": " "}, {"thread": "bad"}, {"thread": "../A-1"},
    {"selected_set": "B-1"}, {"selected_set": "bad"}, {"selected_set": None},
    {"thread": "explore"}, {"thread": "collection"}, {"selected_set": "explore"}, {"extra": SECRET},
])
def test_chat_input_rejected_before_stream(api, changes):
    result = ask(api, **changes)
    assert result.status_code == 422
    assert result.headers["content-type"] == "application/json"
    assert result.json()["code"] == "invalid_request"
    api.responder.assert_not_called()
    assert_safe(result)


def test_sse_progress_is_deduplicated_per_turn(api):
    def respond(question, **kwargs):
        for name, data in [("routing", {}), ("resolving", {}), ("extracting_queries", {}),
                           ("searching", {"q": "q"}), ("selecting", {}),
                           ("fetching", {"title": "제목A"}), ("indexing", {}), ("retrieving", {})]:
            kwargs["on_event"](name, data)
        return answer()

    api.responder.side_effect = respond
    for _ in range(2):
        rows = events(ask(api))
        assert [(row["stage"], row["detail"]) for name, row in rows if name == "stage"] == [
            ("분류", None), ("검색어 추출", None), ("문서 수집", None),
            ("문서 수집", "제목A"), ("검색", None)]
        assert rows[-1][0] == "result"


@pytest.mark.parametrize("subject", [None, "A-1"])
def test_needs_input_is_saved_result(api, subject):
    main.change_subject(api.uid, api.cid, subject, 1)
    message = "API 키가 설정되지 않았습니다. 실행 환경에 키를 설정하세요."
    api.responder.side_effect = None
    api.responder.return_value = orchestrator.OrchestratorResponse(answer=message, needs_input=message)
    result = ask(api)
    assert [name for name, _ in events(result)] == ["result"]
    assert events(result)[0][1]["turn"]["answer"]["needs_input"] == message
    assert api.store.get(api.uid, api.cid)["turns"][0].response["needs_input"] == message
    assert_safe(result)


def test_save_failure_keeps_answer_without_secrets(api):
    api.monkeypatch.setattr(type(api.store), "save_turn_with_state", Mock(side_effect=OSError(SECRET)))
    result = ask(api)
    turn = events(result)[-1][1]["turn"]
    assert turn["id"] == 0 and not turn["saved"]
    assert turn["answer"]["answer"]
    assert "저장하지 못했습니다" in turn["answer"]["warnings"][-1]
    assert_safe(result)
    assert ask(api).status_code == 200


def test_unexpected_chat_exception_one_error_and_unlock(api):
    api.responder.side_effect = RuntimeError(SECRET)
    for _ in range(2):
        result = ask(api)
        assert result.status_code == 200
        assert [name for name, _ in events(result)] == ["error"]
        assert events(result)[0][1]["code"] == "chat_failed"
        assert_safe(result)


def test_thread_start_failure_releases_lock(api):
    original = main.Thread
    api.monkeypatch.setattr(main, "Thread", Mock(side_effect=RuntimeError(SECRET)))
    result = ask(api)
    assert result.status_code == 500
    assert_safe(result)
    api.monkeypatch.setattr(main, "Thread", original)
    assert ask(api).status_code == 200


def test_busy_same_thread_other_thread_and_user_continue(api):
    entered, release = Event(), Event()
    def slow(question, **kwargs):
        if question == "slow":
            entered.set()
            assert release.wait(10)
        return answer()
    api.responder.side_effect = slow
    with ThreadPoolExecutor() as pool:
        pending = pool.submit(ask, api, question="slow")
        try:
            assert entered.wait(5)
            duplicate = ask(api)
            assert duplicate.status_code == 409
            assert duplicate.json() == {"code": "busy", "message": "이전 질문을 처리 중입니다. 답변이 끝난 뒤 다시 질문하세요."}
            assert api.client.delete(f"/api/conversations/{api.cid}", headers=api.headers).status_code == 409
            assert ask(api, conversation_id=api.store.create(api.uid)).status_code == 200
            other = api.client.post("/api/users").json()["uid"]
            assert api.client.post("/api/chat", headers={"X-User-Id": other},
                                   json={"conversation_id": api.store.create(other), "request_id": "other", "question": "다른 사용자"}).status_code == 200
        finally:
            release.set()
        assert events(pending.result(timeout=5))[-1][0] == "result"
    assert ask(api).status_code == 200


def test_disconnect_first_stage_finishes_saves_and_unlocks(api):
    release, finished = Event(), Event()
    def slow(question, **kwargs):
        kwargs["on_event"]("routing", {})
        assert release.wait(10)
        return answer()
    api.responder.side_effect = slow
    original_release = api.app.state.turn_locks.release
    def unlock(uid, thread):
        original_release(uid, thread)
        finished.set()
    api.monkeypatch.setattr(api.app.state.turn_locks, "release", unlock)

    async def disconnect():
        disconnected = asyncio.Event()
        request_sent = False
        messages = []
        async def receive():
            nonlocal request_sent
            if not request_sent:
                request_sent = True
                return {"type": "http.request", "body": json.dumps({"conversation_id": api.cid, "request_id": "disconnect", "question": "연결 중단"}).encode(), "more_body": False}
            await disconnected.wait()
            return {"type": "http.disconnect"}
        async def send(message):
            messages.append(message)
            if b"event: stage" in message.get("body", b""):
                disconnected.set()
        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.0"}, "http_version": "1.1",
                 "method": "POST", "scheme": "http", "path": "/api/chat", "raw_path": b"/api/chat",
                 "query_string": b"", "root_path": "", "headers": [(b"content-type", b"application/json"),
                 (b"x-user-id", api.uid.encode())], "server": ("testserver", 80), "client": ("test", 1)}
        await asyncio.wait_for(api.app(scope, receive, send), timeout=5)
        assert disconnected.is_set()
        assert not release.is_set() and not finished.is_set()
        assert not any(b"event: result" in message.get("body", b"") for message in messages)

    try:
        # TestClient는 응답을 버퍼링하므로 실제 ASGI disconnect 메시지를 주입한다.
        asyncio.run(disconnect())
        assert ask(api, request_id="disconnect").status_code == 409
    finally:
        release.set()
    assert finished.wait(5)
    rows = api.client.get(f"/api/conversations/{api.cid}", headers=api.headers).json()["turns"]
    assert len(rows) == 1 and rows[0]["question"] == "연결 중단" and rows[0]["saved"]
    replay = events(ask(api, request_id="disconnect"))
    assert [name for name, _ in replay] == ["result"]
    assert replay[0][1]["turn"] == rows[0]
    assert len(api.store.get(api.uid, api.cid)["turns"]) == 1


def test_keepalive_while_service_is_running(api):
    # 시간 경과는 서비스 대역이 기다리며, 주석이 전송된 것을 관찰해 작업을 풀어준다.
    release = Event()
    def slow(question, **kwargs):
        assert release.wait(5)
        return answer()
    api.responder.side_effect = slow
    api.app.state.server_settings["keepalive_sec"] = 0.01
    original = main.format_sse_event
    def observe(**kwargs):
        if kwargs.get("comment") == "keep-alive":
            release.set()
        return original(**kwargs)
    api.monkeypatch.setattr(main, "format_sse_event", observe)
    result = ask(api)
    assert ": keep-alive\n\n" in result.text
    assert [name for name, _ in events(result)] == ["result"]
