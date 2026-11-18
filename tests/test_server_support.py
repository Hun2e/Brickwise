"""서버 테스트용 로컬 DB와 외부 호출 없는 공통 서비스 대역."""

import json
import sqlite3
from uuid import uuid4
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from rag_service.config import load_settings
from rag_service.rag.evidence import Card
from rag_service.services import catalog, chat, collection_actions, conversations, orchestrator, status, users
from rag_service.services.catalog import SetOption
from server import main

SECRET = "secret-key-123 system-prompt Traceback internal-path"


def answer() -> orchestrator.OrchestratorResponse:
    return orchestrator.OrchestratorResponse(
        route="fact", answer="부품 수입니다. [근거 7]",
        cards=[Card(7, "db", "DB", "https://rebrickable.com/", "DB", "값"),
               Card(8, "wikipedia", "미인용", "", "", "미인용 텍스트")],
        tables=[{"title": "표", "columns": ["값"], "rows": [[1]], "formats": {}}],
        set_nums=["A-1"], timings={"total_ms": 1}, usage={"private": SECRET},
        route_source=SECRET, verification={"private": SECRET},
    )


def events(response) -> list[tuple[str, dict]]:
    result = []
    for block in response.text.replace("\r\n", "\n").split("\n\n"):
        lines = block.splitlines()
        name = next((line[7:] for line in lines if line.startswith("event: ")), None)
        if name:
            result.append((name, json.loads(next(line[6:] for line in lines if line.startswith("data: ")))))
    return result


@pytest.fixture
def api(tmp_path, monkeypatch):
    cfg = load_settings()
    cfg["paths"].update(user_sqlite=str(tmp_path / "user.sqlite"),
                        sqlite=str(tmp_path / "missing.sqlite"), chroma_dir=str(tmp_path / "chroma"))
    monkeypatch.setattr(users, "load_settings", lambda: cfg)
    monkeypatch.setattr(chat, "load_settings", lambda: cfg)
    monkeypatch.setattr(status, "load_settings", lambda: cfg)
    monkeypatch.setattr(status, "load_env", lambda: None)
    monkeypatch.delenv("GMS_KEY", raising=False)
    monkeypatch.setattr(orchestrator, "log_exception", Mock())
    monkeypatch.setattr(main, "log_exception", Mock())
    options = [SetOption("A-1", "Falcon", 2020, 100, "core", ["팔콘", "밀레니엄 팔콘"], "https://example.com/a.png"),
               SetOption("B-1", "X-wing", 2021, 2000, "compare_only", ["엑스윙"], "")]
    option_mock = Mock(return_value=options)
    monkeypatch.setattr(main, "catalog_options", option_mock)
    monkeypatch.setattr(catalog, "catalog_options", option_mock)
    # 상태 확인 테스트의 DB 누락은 status 설정에만 남기고 채팅은 임시 참조 DB를 쓴다.
    chat_cfg = {**cfg, "paths": {**cfg["paths"], "sqlite": str(tmp_path / "reference.sqlite")}}
    with sqlite3.connect(chat_cfg["paths"]["sqlite"]) as db:
        db.execute("CREATE TABLE sets(set_num TEXT, name TEXT, year INT, theme_id INT, num_parts INT, img_url TEXT)")
        db.execute("CREATE TABLE set_root_theme(set_num TEXT, root_theme_name TEXT)")
        db.execute("CREATE TABLE set_scope(set_num TEXT, scope TEXT)")
        db.executemany("INSERT INTO sets VALUES (?, ?, ?, 1, ?, '')",
                       [(o.set_num, o.name, o.year, o.num_parts) for o in options])
        db.executemany("INSERT INTO set_scope VALUES (?, ?)",
                       [(o.set_num, o.scope) for o in options])
    monkeypatch.setattr(chat, "load_settings", lambda path=None: load_settings(path) if path else chat_cfg)
    monkeypatch.setattr(collection_actions, "load_settings", lambda path=None: load_settings(path) if path else chat_cfg)
    repo = conversations.ConversationRepo(tmp_path / "user.sqlite")

    def respond(question, **kwargs):
        for name, data in [("routing", {}), ("fetching", {"title": "Falcon"}),
                           ("generating", {}), ("verifying", {})]:
            kwargs["on_event"](name, data)
        return answer()

    responder = Mock(side_effect=respond)
    monkeypatch.setattr(orchestrator, "answer", responder)
    app = main.create_app()
    with TestClient(app, raise_server_exceptions=False) as client:
        uid = client.post("/api/users").json()["uid"]
        cid = repo.create(uid)
        chat.change_subject(uid, cid, "A-1", 0)
        yield SimpleNamespace(client=client, app=app, uid=uid, headers={"X-User-Id": uid},
                              store=repo, cid=cid, cfg=cfg,
                              responder=responder, options=option_mock, monkeypatch=monkeypatch)


def ask(api, **changes):
    body = {"conversation_id": api.cid, "request_id": uuid4().hex, "question": "부품 수는?"}
    body.update(changes)
    return api.client.post("/api/chat", headers=api.headers, json=body)


def assert_safe(response):
    assert all(word not in response.text for word in SECRET.split())
