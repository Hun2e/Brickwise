import ast
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from rag_service.services import catalog, conversations, users
from server import main
from server.deps import SERVER_SETTINGS
from test_server_support import SECRET, api, ask, assert_safe


def test_public_routes_and_catalog_cache(api):
    client = api.client
    assert client.get("/api/health").json() == {"status": "ok"}
    assert users.valid_user_id(client.post("/api/users").json()["uid"])
    assert client.post("/api/users").json()["uid"] != api.uid
    assert '<script type="module" src="/js/app.js">' in client.get("/").text
    assert client.get("/docs").status_code == 200
    result = client.get("/api/sets")
    assert result.headers["cache-control"] == f"max-age={SERVER_SETTINGS['catalog_max_age_sec']}"
    rows = result.json()
    assert rows[0]["aliases"] == ["팔콘", "밀레니엄 팔콘"]
    assert rows[0]["label"] == "A-1 · Falcon (2020) · 팔콘"
    assert rows[1]["label"].endswith("(비교용)")
    assert set(rows[0]) == {"set_num", "name", "year", "num_parts", "scope", "aliases", "img_url", "label"}
    assert client.get("/api/sets").json() == rows
    api.options.assert_called_once()


def test_missing_db_and_key_status_is_safe(api):
    result = api.client.get("/api/status")
    assert result.status_code == 200
    assert result.json()["user_db_ok"]
    assert not result.json()["can_chat"]
    assert not result.json()["db_ok"] and not result.json()["api_key_set"]
    assert any("GMS_KEY" in text for text in result.json()["messages"])
    assert_safe(result)


@pytest.mark.parametrize("method,path", [
    ("get", "/api/threads"), ("get", "/api/threads/A-1/turns"),
    ("delete", "/api/threads/A-1"), ("get", "/api/collection"),
    ("put", "/api/collection"), ("post", "/api/chat"),
])
@pytest.mark.parametrize("uid", [None, "bad-id"])
def test_uid_required_and_safe(api, method, path, uid):
    response = api.client.request(method, path, headers={} if uid is None else {"X-User-Id": uid},
                                  json={"sets": [], "version": 2} if method == "put" else
                                  {"thread": "explore", "question": "질문"})
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_user_id"
    assert set(response.json()) == {"code", "message"}


def test_collection_replace_clear_and_user_isolation(api):
    client, headers = api.client, api.headers
    assert client.get("/api/collection", headers=headers).json() == {"id": api.uid, "sets": [], "adjustments": [], "version": 0}
    result = client.put("/api/collection", headers=headers, json={"sets": [["A-1", 2], ["B-1", 1]], "version": 0})
    assert result.status_code == 200
    assert users.load_collection(api.uid)["sets"] == [("A-1", 2), ("B-1", 1)]
    other = {"X-User-Id": users.new_user_id()}
    assert client.get("/api/collection", headers=other).json()["sets"] == []
    assert client.put("/api/collection", headers=headers, json={"sets": [["B-1", 3]], "version": 1}).json()["sets"] == [["B-1", 3]]
    assert client.put("/api/collection", headers=headers, json={"sets": [], "version": 2}).json()["sets"] == []


@pytest.mark.parametrize("sets", [[["A-1", 0]], [["A-1", True]], [["A-1", "2"]], [["A-1", 1.5]],
                                  [["missing-1", 1]], [["explore", 1]], [["../bad", 1]]])
def test_collection_bad_input(api, sets):
    result = api.client.put("/api/collection", headers=api.headers, json={"sets": sets, "version": 0})
    assert result.status_code == 422
    assert set(result.json()) == {"code", "message"}


def test_shared_store_history_and_clear(api):
    from rag_service.services import chat_turn_v2, get_conversation, change_subject
    ask(api)
    version = get_conversation(api.uid, api.cid)["version"]
    change_subject(api.uid, api.cid, "B-1", version)
    chat_turn_v2(api.uid, api.cid, "다른 세트", request_id="b")
    assert api.responder.call_args.kwargs["history"] == []
    version = get_conversation(api.uid, api.cid)["version"]
    change_subject(api.uid, api.cid, "A-1", version)
    chat_turn_v2(api.uid, api.cid, "Streamlit 질문", request_id="a")
    assert [t["question"] for t in api.responder.call_args.kwargs["history"]] == ["부품 수는?"]
    turns = api.client.get("/api/sets/A-1/history", headers=api.headers).json()
    assert [row["question"] for row in turns] == ["부품 수는?", "Streamlit 질문"]
    assert len(api.client.get("/api/sets/B-1/history", headers=api.headers).json()) == 1
    assert api.client.get("/api/conversations", headers={"X-User-Id": users.new_user_id()}).json() == []
    assert api.client.delete(f"/api/conversations/{api.cid}", headers=api.headers).json() == {"deleted": 3}
    assert api.client.get("/api/sets/A-1/history", headers=api.headers).json() == []
    assert api.client.get(f"/api/conversations/{api.cid}", headers=api.headers).status_code == 404


@pytest.mark.parametrize("method,path", [("get", "/api/threads/bad/turns"), ("delete", "/api/threads/bad")])
def test_invalid_thread_path(api, method, path):
    result = api.client.request(method, path, headers=api.headers)
    assert result.status_code == 422
    assert result.json()["code"] == "invalid_request"


@pytest.mark.parametrize("function,path", [("catalog_options", "/api/sets"), ("check_status", "/api/status"),
                                          ("legacy_threads", "/api/threads"), ("load_collection", "/api/collection")])
def test_http_unexpected_errors_do_not_leak(api, function, path):
    api.monkeypatch.setattr(main, function, Mock(side_effect=OSError(SECRET)))
    result = api.client.get(path, headers=api.headers)
    assert result.status_code == 500
    assert set(result.json()) == {"code", "message"}
    assert_safe(result)


def test_catalog_failure_is_not_cached(api):
    api.options.side_effect = [OSError(SECRET), api.options.return_value]
    assert api.client.get("/api/sets").status_code == 500
    assert api.client.get("/api/sets").status_code == 200
    assert api.options.call_count == 2


def test_http_errors_and_malformed_json_use_error_out(api):
    for response in [api.client.get("/api/missing"), api.client.post("/api/health"),
                     api.client.post("/api/chat", headers=api.headers, content=SECRET)]:
        assert response.status_code in (404, 405, 422)
        assert set(response.json()) == {"code", "message"}
        assert_safe(response)


def test_cors_only_when_configured(api):
    headers = {"Origin": "https://frontend.example", "Access-Control-Request-Method": "POST",
               "Access-Control-Request-Headers": "X-User-Id,Content-Type"}
    assert "access-control-allow-origin" not in api.client.options("/api/chat", headers=headers).headers
    api.monkeypatch.setitem(SERVER_SETTINGS, "cors_origins", ["https://frontend.example"])
    with TestClient(main.create_app()) as client:
        result = client.options("/api/chat", headers=headers)
        assert result.status_code == 200
        assert result.headers["access-control-allow-origin"] == "https://frontend.example"


def test_openapi_contract(api):
    spec = api.client.get("/openapi.json").json()
    assert "/api/conversations/{cid}/subject" in spec["paths"]
    schemas = spec["components"]["schemas"]
    assert {"ChatIn", "StageEvent", "TurnOut", "AnswerOut", "CardOut", "TableOut", "ErrorOut",
            "CollectionIn", "CollectionOut", "StatusOut", "SetOut", "ThreadOut", "UserOut", "ClearOut", "HealthOut", "ConversationOut", "StateSummaryOut", "ItemOut", "ActionOut", "SubjectIn", "ChatResultOut"} <= schemas.keys()
    assert schemas["ChatIn"]["properties"]["question"]["maxLength"] == SERVER_SETTINGS["max_question_chars"]
    assert not {"usage", "verification", "route_source"} & schemas["AnswerOut"]["properties"].keys()
    operation = spec["paths"]["/api/chat"]["post"]
    assert operation["responses"]["422"]["content"]["application/json"]["schema"]["$ref"].endswith("/ErrorOut")
    payload = operation["responses"]["200"]["content"]["text/event-stream"]["schema"]
    assert payload["type"] == "object"
    assert {ref["$ref"].split("/")[-1] for ref in payload["anyOf"]} == {"StageEvent", "ChatResultOut", "ErrorOut"}


def test_only_public_service_imports():
    for path in Path("server").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("rag_service"):
                assert node.module == "rag_service.services" or node.module.startswith("rag_service.services.")
                assert all(not name.name.startswith("_") for name in node.names)
            elif isinstance(node, ast.Import):
                assert all(not name.name.startswith("rag_service") for name in node.names)
