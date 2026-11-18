import json
from dataclasses import asdict
from unittest.mock import Mock

import pytest

from rag_service.services import chat, orchestrator
from rag_service.services.conversations import ConversationStore
from rag_service.services.users import new_user_id
from rag_service.rag.evidence import Card


def response():
    return orchestrator.OrchestratorResponse(
        route="fact", answer="응답 [근거 7]", cards=[Card(7, "db", "제목", "https://rebrickable.com/", "DB", "값")],
        tables=[{"title": "표", "columns": ["값"], "rows": [[1]], "formats": {}}],
        set_nums=["A-1"], warnings=["경고"], timings={"total_ms": 5}, usage={"router": {"calls": 1}},
        verification={"checked": 1, "removed": 0, "items": [{"value": "1"}]},
    )


def test_full_json_roundtrip():
    r = response()
    assert json.loads(json.dumps(chat.response_to_dict(r))) == asdict(r)
    r.cards.append(Card(8, "db", "미인용", "", "", ""))
    assert [c["n"] for c in chat.cited_cards(chat.response_to_dict(r))] == [7]


def test_history_context_stages_and_store(tmp_path, monkeypatch):
    uid, store = new_user_id(), ConversationStore(tmp_path / "user.sqlite")
    for i in range(6):
        store.append(uid, "A-1", str(i), {"answer": str(i)})
    store.append(uid, "B-1", "다른 세트", {})
    calls, stages = [], []
    def fake(question, **kwargs):
        calls.append((question, kwargs))
        for event, data in [("routing", {}), ("fetching", {"title": "Falcon"}),
                            ("generating", {}), ("verifying", {}), ("done", {})]:
            kwargs["on_event"](event, data)
        return response()
    monkeypatch.setattr(orchestrator, "answer", fake)
    turn = chat.chat_turn(uid, "A-1", "질문", selected_set="A-1", store=store,
                          on_stage=lambda *args: stages.append(args))
    kwargs = calls[0][1]
    assert kwargs["collection_id"] == uid and kwargs["selected_set"] == "A-1"
    assert kwargs["history"] == [{"question": str(i), "answer": str(i)} for i in range(2, 6)]
    assert stages == [("분류", None), ("문서 수집", "Falcon"), ("생성", None), ("검증", None)]
    assert store.turns(uid, "A-1")[-1] == turn


@pytest.mark.parametrize("thread", ["explore", "collection"])
def test_needs_input_saved(tmp_path, monkeypatch, thread):
    store = ConversationStore(tmp_path / "user.sqlite")
    fake = Mock(return_value=orchestrator.OrchestratorResponse(answer="추가 입력", needs_input="추가 입력"))
    monkeypatch.setattr(orchestrator, "answer", fake)
    uid = new_user_id()
    turn = chat.chat_turn(uid, thread, "질문", selected_set=None, store=store)
    assert turn.response["needs_input"] == "추가 입력"
    assert store.turns(uid, thread) == [turn]
    assert fake.call_args.kwargs["selected_set"] is None


def test_failed_storage_keeps_answer_and_safe_warning(monkeypatch):
    store = Mock()
    store.turns.return_value = []
    store.append.side_effect = OSError("secret system prompt")
    monkeypatch.setattr(orchestrator, "answer", Mock(return_value=response()))
    monkeypatch.setattr(orchestrator, "log_exception", Mock())
    turn = chat.chat_turn(new_user_id(), "A-1", "질문", selected_set="A-1", store=store)
    assert turn.id == 0 and turn.response["answer"] == "응답 [근거 7]"
    assert "저장하지 못했습니다" in turn.response["warnings"][-1]
    assert "secret" not in str(turn)


def test_failed_read_keeps_response_and_saves(monkeypatch):
    store = Mock()
    store.turns.side_effect = OSError("secret")
    fake = Mock(return_value=response())
    monkeypatch.setattr(orchestrator, "answer", fake)
    monkeypatch.setattr(orchestrator, "log_exception", Mock())
    chat.chat_turn(new_user_id(), "A-1", "질문", selected_set="A-1", store=store)
    assert fake.call_args.kwargs["history"] == []
    assert "이전 대화를 읽지 못했습니다" in store.append.call_args.args[3]["warnings"][-1]


def test_mismatched_selected_set_rejected(monkeypatch):
    fake = Mock()
    monkeypatch.setattr(orchestrator, "answer", fake)
    with pytest.raises(ValueError):
        chat.chat_turn(new_user_id(), "A-1", "질문", selected_set="B-1")
    fake.assert_not_called()
