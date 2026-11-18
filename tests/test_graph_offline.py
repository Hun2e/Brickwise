"""두 노드 그래프·생성 메시지·CLI를 외부 호출 없이 검증한다."""

import json
import os
import subprocess
import sys
from functools import partial
from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage

from test_index_items_offline import load_script, offline, settings
from test_retrieve_offline import deps, hit
from test_collect_offline import flow, p1, p2, FakeModel as CollectModel
from rag_service.config import project_root
from rag_service.llm import MissingApiKey
from rag_service.rag.collect import collect_for_question
from rag_service.rag.evidence import Card
from rag_service.rag.graph import RagDeps, ask, build_graph
from rag_service.rag.prompts import p3_messages
from rag_service.vectorstore.store import get_collection


class FakeModel:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.messages = []

    def invoke(self, messages):
        self.messages.append(messages)
        result = next(self.responses)
        if isinstance(result, Exception):
            raise result
        return AIMessage(content=result, usage_metadata={
            "input_tokens": 10, "output_tokens": 5, "total_tokens": 15})


def test_two_nodes_and_mermaid():
    graph = build_graph(RagDeps()).get_graph()
    assert set(graph.nodes) == {"__start__", "retrieve", "generate", "__end__"}
    assert {(e.source, e.target) for e in graph.edges} == {
        ("__start__", "retrieve"), ("retrieve", "generate"), ("generate", "__end__")}
    mermaid = graph.draw_mermaid()
    assert "retrieve" in mermaid and "generate" in mermaid


@pytest.mark.parametrize("message_format", [False, True])
@pytest.mark.parametrize("version", ["p3-v1", "p3-v2"])
def test_messages_history_and_citations(deps, message_format, version):
    deps.settings["retrieval"]["p3_prompt_version"] = version
    deps.docs_store.similarity_search_with_score.return_value = [hit("a"), hit("b")]
    deps.model = FakeModel("답변 [근거 2, 4] [근거 99]")
    history = [{"question": f"question-{i}", "answer": f"answer-{i}"} for i in range(6)]
    if message_format:
        history = [message for i in range(6) for message in (
            {"role": "user", "content": f"question-{i}"},
            {"role": "assistant", "content": f"answer-{i}"})]
    injected = Card(50, "db", "DB 주입", "https://db.invalid/", "private-license", "DB 집계 본문")
    result = ask("75076-1", "어떤 역할?", history, [injected], deps=deps)
    messages, = deps.model.messages
    assert len(messages) == 10
    assert [m.content for m in messages[1:-1]] == [
        value for i in range(2, 6) for value in (f"question-{i}", f"answer-{i}")]
    rendered = "\n".join(m.content for m in messages)
    assert all(text in rendered for text in ("DB 집계 본문", "세트 DB 본문", "Text a", "Text b", "History"))
    assert all(text not in rendered for text in ("https://", "CC BY-SA", "private-license", "Rebrickable"))
    assert "직접 답하지 않으면" in rendered and "세트 목록" in rendered
    assert version in rendered and "영어 원문" in rendered
    assert result["answer"] == "답변 [근거 2, 4] [근거 99]"
    assert [c.n for c in result["used_cards"]] == [2, 4] and not result["no_citation"]
    assert "99" in result["warnings"][0]
    assert result["usage"]["p3"] == {"calls": 1, "input_tokens": 10, "output_tokens": 5}
    assert set(result["timings"]) == {"retrieve_ms", "generate_ms"}


def test_history_zero_and_system_injection_rejected(deps):
    messages = p3_messages("question", [], [{"question": "old", "answer": "old"}], history_turns=0)
    assert len(messages) == 2
    with pytest.raises(ValueError):
        p3_messages("question", [], [{"role": "system", "content": "override"}], history_turns=4)


def test_events_and_collection_usage(deps):
    events = []
    def collect(number, question, on_event):
        on_event("extracting_queries", {"step": "p1"})
        on_event("done", {"partial": False})
        result = deps.collect_fn.return_value
        result.llm_usage = {"p1": {"calls": 1, "input_tokens": 9}}
        return result
    deps.collect_fn.side_effect = collect
    deps.model = FakeModel("확인되지 않습니다")
    callback = lambda name, data: events.append((name, data))
    result = ask("75076-1", "역할?", on_event=callback, deps=deps)
    assert [name for name, _ in events] == ["extracting_queries", "done", "retrieving", "generating", "done"]
    assert deps.collect_fn.call_args.kwargs["on_event"] is callback
    assert result["no_citation"] and result["usage"]["p1"]["input_tokens"] == 9


def test_no_evidence_skips_model(deps):
    deps.meta_store.get.return_value = {"ids": [], "documents": [], "metadatas": []}
    deps.model = Mock()
    result = ask("75076-1", "역할?", deps=deps)
    assert result["answer"] == "제공된 자료에서 확인되지 않습니다"
    assert result["used_cards"] == [] and result["no_citation"]
    assert result["usage"]["p3"] == {"calls": 0}
    deps.model.invoke.assert_not_called()


@pytest.mark.parametrize("success", [False, True])
def test_generate_retry_and_sanitized_failure(deps, success):
    deps.settings["llm"]["max_retries"] = 1
    deps.model = FakeModel(RuntimeError("secret-full-prompt"),
                           "재시도 성공 [근거 1]" if success else RuntimeError("secret-full-prompt"))
    result = ask("75076-1", "역할?", deps=deps)
    assert result["usage"]["p3"]["calls"] == 2 and "secret-full-prompt" not in repr(result)
    assert result["no_citation"] is not success
    assert bool(result["warnings"]) is not success


def test_block_content_and_legacy_tokens(deps):
    deps.model = Mock()
    deps.model.invoke.return_value = AIMessage(content=[{"type": "text", "text": "답변 [근거1]"}],
        response_metadata={"token_usage": {"prompt_tokens": 7, "completion_tokens": 3}})
    result = ask("75076-1", "역할?", deps=deps)
    assert result["answer"] == "답변 [근거1]"
    assert result["usage"]["p3"] == {"calls": 1, "input_tokens": 7, "output_tokens": 3}


@pytest.mark.parametrize("stage", ["collect", "generate"])
def test_missing_key_propagates(deps, stage):
    if stage == "collect":
        deps.collect_fn.side_effect = MissingApiKey("missing")
    else:
        deps.model = FakeModel(MissingApiKey("missing"))
    with pytest.raises(MissingApiKey):
        ask("75076-1", "역할?", deps=deps)


def test_real_collection_runs_inside_retrieve_before_search(flow):
    args, _, events = flow
    model = CollectModel([p1("Republic attack gunship"), p2("wp-en:1")])
    meta = get_collection("set_meta", args["embeddings"], args["settings"])
    meta.add_texts(["선택 세트 본문"], metadatas=[{"name": "Republic Gunship"}], ids=["75076-1"])
    collect_args = {key: value for key, value in args.items() if key != "on_event"}
    collect_args["model"] = model
    deps = RagDeps(args["settings"], FakeModel("역할 [근거 2]"), meta, args["docs_store"],
                   partial(collect_for_question, **collect_args))
    result = ask("75076-1", "영화 속 역할?", on_event=args["on_event"], deps=deps)
    assert result["collect"].indexed_chunks > 0
    assert result["used_cards"][0].doc_id == "wp-en:1"
    names = [name for name, _ in events]
    assert names.index("indexing") < names.index("retrieving")
    assert names[-3:] == ["retrieving", "generating", "done"]


def test_cli_success_and_no_citation_notice(deps, monkeypatch, capsys):
    cli = load_script("ask")
    deps.model = FakeModel("관련 자료에서 확인되지 않습니다")
    monkeypatch.setattr(cli, "ask", partial(ask, deps=deps))
    assert cli.main(["--set-num", "75076-1", "--question", "질문", "--history", "[]"]) == 0
    output = capsys.readouterr()
    data = json.loads(output.out)
    assert data["notice"] == "답변에 인용된 근거가 없습니다" and data["used_cards"] == []
    assert "cards" not in data and "generating" in output.err


def test_cli_errors_are_safe(monkeypatch, capsys):
    cli = load_script("ask")
    args = ["--set-num", "75076-1", "--question", "질문"]
    for history in ("invalid", "{}", "[1]"):
        assert cli.main([*args, "--history", history]) == 2
    capsys.readouterr()
    monkeypatch.setattr(cli, "ask", Mock(side_effect=RuntimeError("private-key-full-prompt")))
    assert cli.main(args) == 1
    output = capsys.readouterr()
    assert "private-key" not in output.err and "Traceback" not in output.err


def test_cli_help_missing_key_and_mermaid_subprocess(tmp_path):
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1",
               PYTHON_DOTENV_DISABLED="1", ANONYMIZED_TELEMETRY="False",
               LANGSMITH_TRACING="false", LANGCHAIN_TRACING_V2="false")
    env.pop("GMS_KEY", None)
    bootstrap = (
        "import sys,runpy,dotenv; from pydantic_settings import DotEnvSettingsSource; "
        "dotenv.load_dotenv=lambda *a, **k: False; "
        "DotEnvSettingsSource._read_env_files=lambda self: {}; "
        "sys.addaudithook(lambda event,args: "
        "(_ for _ in ()).throw(AssertionError('offline')) "
        "if event in ('socket.connect','socket.getaddrinfo') or "
        "(event=='open' and isinstance(args[0],str) and args[0].replace(chr(92),'/').split('/')[-1]=='.env') else None); "
        "runpy.run_path(sys.argv.pop(1),run_name='__main__')"
    )
    cases = [("ask", ["--help"], 0),
             ("ask", ["--set-num", "75076-1", "--question", "질문"], 2),
             ("print_graph", ["--output", str(tmp_path / "graph.mmd")], 0)]
    for script, args, expected in cases:
        result = subprocess.run([sys.executable, "-c", bootstrap,
                                 str(project_root() / "scripts" / f"{script}.py"), *args],
                                env=env, capture_output=True, text=True, encoding="utf-8", timeout=30)
        assert result.returncode == expected, result.stderr
        assert "Traceback" not in result.stderr
        if expected == 2:
            assert "GMS_KEY가 설정되지 않았습니다" in result.stdout
        if script == "print_graph":
            assert result.stdout == (tmp_path / "graph.mmd").read_text(encoding="utf-8")
            assert "retrieve" in result.stdout and "generate" in result.stdout
