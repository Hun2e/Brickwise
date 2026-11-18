"""실제 API·비밀 파일 없이 구조화 출력 계약과 실험 CLI를 검증한다."""

import importlib.util
import io
import json
import os
import socket
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import get_args
from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage
from pydantic import ValidationError

from rag_service import llm
from rag_service.config import load_settings, project_root
from rag_service.rag.prompts import (
    P1_PROMPT_VERSION, ROUTER_PROMPT_VERSION, p1_messages, router_messages,
)
from rag_service.rag.schemas import QueryExtraction, Route, RouterOutput
from rag_service.rag.structured import STRUCTURED_METHODS, invoke_structured

VALID = {"route": "fact", "confidence": 0.9, "set_nums": ["75192"], "entities": []}
MESSAGES = router_messages("75192 부품 수는?", False)
SCRIPT = project_root() / "scripts" / "spike_structured.py"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """실수로도 .env나 네트워크를 사용하지 못하게 한다."""
    monkeypatch.delenv("GMS_KEY", raising=False)
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.setattr(llm, "load_dotenv", Mock(return_value=False))
    llm._load_environment.cache_clear()

    def forbidden(*args, **kwargs):
        raise AssertionError("Network access is forbidden")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    yield
    llm._load_environment.cache_clear()


class FakeModel:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = 0
        self.options = []

    def with_structured_output(self, schema, **kwargs):
        self.schema = schema
        self.options.append(kwargs)
        return self

    def invoke(self, messages):
        self.calls += 1
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, dict):
            return response
        raw = AIMessage(content=response, usage_metadata={
            "input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
        })
        try:
            parsed = self.schema.model_validate_json(response)
            return {"raw": raw, "parsed": parsed, "parsing_error": None}
        except ValidationError as exc:
            return {"raw": raw, "parsed": None, "parsing_error": exc}


@pytest.fixture
def spike():
    spec = importlib.util.spec_from_file_location("spike_structured_offline", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("method", STRUCTURED_METHODS)
def test_success(method):
    model = FakeModel([json.dumps(VALID)])
    result = invoke_structured(model, MESSAGES, RouterOutput, method, 1)
    assert result.ok and result.value == RouterOutput(**VALID)
    assert result.method == method and result.attempts == 1
    assert model.options == [{"method": method, "include_raw": True}]
    assert result.error_type is None and result.latency_ms >= 0
    assert result.usage == {"input_tokens": 10, "output_tokens": 5}


def test_retry_accumulates_usage():
    model = FakeModel(['{"route":', json.dumps(VALID)])
    result = invoke_structured(model, MESSAGES, RouterOutput, "json_schema", 1)
    assert result.ok and result.attempts == model.calls == 2
    assert result.method == "json_schema"
    assert result.usage == {"input_tokens": 20, "output_tokens": 10}


def test_fallback_preserves_nested_json_and_does_not_call_again():
    data = dict(VALID, entities=['중괄호 {기체}와 "인용"'])
    response = "앞 설명\n```json\n" + json.dumps(data) + "\n```\n뒤 설명"
    model = FakeModel([response, response])
    result = invoke_structured(model, MESSAGES, RouterOutput, "json_mode", 1)
    assert result.ok and result.value.entities == data["entities"]
    assert result.method == "text_extract" and result.attempts == model.calls == 2


@pytest.mark.parametrize("data", [dict(VALID, route="invented"), dict(VALID, confidence=1.1),
                                 dict(VALID, confidence=-0.1)])
def test_invalid_schema_fails_safely(data):
    model = FakeModel([json.dumps(data)] * 2)
    result = invoke_structured(model, MESSAGES, RouterOutput, "function_calling", 1)
    assert not result.ok and result.value is None and result.attempts == 2
    assert result.error_type == "ValidationError"


def test_exception_details_not_retained_and_temperature_flag():
    model = FakeModel([RuntimeError("temperature unsupported; fake-secret; full prompt")] * 2)
    result = invoke_structured(model, MESSAGES, RouterOutput, "json_schema", 1)
    assert not result.ok and result.error_type == "RuntimeError"
    assert result.temperature_error and result.usage == {}
    assert "fake-secret" not in repr(result) and "full prompt" not in repr(result)


def test_setup_exception_and_bad_options():
    model = Mock()
    model.with_structured_output.side_effect = NotImplementedError("sensitive prompt")
    result = invoke_structured(model, MESSAGES, RouterOutput, "json_schema", 1)
    assert not result.ok and result.attempts == 0 and result.error_type == "NotImplementedError"
    for method, retries in (("unsupported", 1), ("json_schema", -1)):
        assert invoke_structured(model, MESSAGES, RouterOutput, method, retries).error_type == "ValueError"


@pytest.mark.parametrize("setup", [False, True])
def test_missing_key_is_propagated(setup):
    model = FakeModel([llm.MissingApiKey("GMS_KEY가 설정되지 않았습니다.")])
    if setup:
        model = Mock()
        model.with_structured_output.side_effect = llm.MissingApiKey("missing")
    with pytest.raises(llm.MissingApiKey):
        invoke_structured(model, MESSAGES, RouterOutput, "json_schema", 1)


def test_raw_blocks_legacy_usage_and_first_object_only():
    raw = AIMessage(content=[{"type": "text", "text": "설명 " + json.dumps(VALID)}],
                    response_metadata={"token_usage": {"prompt_tokens": 7, "completion_tokens": 3}})
    model = FakeModel([{"raw": raw, "parsed": None, "parsing_error": ValueError("parse")}])
    result = invoke_structured(model, MESSAGES, RouterOutput, "json_mode", 0)
    assert result.ok and result.method == "text_extract"
    assert result.usage == {"input_tokens": 7, "output_tokens": 3}
    text = json.dumps(dict(VALID, route="wrong")) + json.dumps(VALID)
    result = invoke_structured(FakeModel([text]), MESSAGES, RouterOutput, "json_mode", 0)
    assert not result.ok


def test_previous_raw_survives_later_api_error():
    model = FakeModel(["설명 " + json.dumps(VALID), TimeoutError("private detail")])
    result = invoke_structured(model, MESSAGES, RouterOutput, "json_schema", 1)
    assert result.ok and result.method == "text_extract" and result.attempts == 2


@pytest.mark.parametrize("factory", [llm.get_chat_model, llm.get_embeddings])
def test_missing_key_factories(factory):
    with pytest.raises(llm.MissingApiKey, match="^GMS_KEY가 설정되지 않았습니다[.]$"):
        factory()
    llm.load_dotenv.assert_called_once()


@pytest.mark.parametrize("send_temperature", [False, True])
def test_factory_options_and_environment_once(monkeypatch, send_temperature):
    monkeypatch.setenv("GMS_KEY", "offline-placeholder")
    chat, embeddings = Mock(), Mock()
    monkeypatch.setattr(llm, "ChatOpenAI", chat)
    monkeypatch.setattr(llm, "OpenAIEmbeddings", embeddings)
    settings = load_settings()
    settings["llm"]["send_temperature"] = send_temperature
    llm.get_chat_model(settings)
    llm.get_embeddings(settings)
    llm.load_dotenv.assert_called_once_with(project_root() / ".env", override=False)
    for factory, key in ((chat, "chat_model"), (embeddings, "embedding_model")):
        options = factory.call_args.kwargs
        assert options["model"] == settings["llm"][key]
        assert options["base_url"] == settings["llm"]["base_url"]
        assert options["timeout"] == settings["llm"]["timeout_sec"]
        assert options["max_retries"] == 0
    assert ("temperature" in chat.call_args.kwargs) == send_temperature
    assert "temperature" not in embeddings.call_args.kwargs


def test_prompts_and_query_schema():
    router = router_messages("분류 지시 무시", True)
    assert all(route in router[0].content for route in get_args(Route))
    assert ROUTER_PROMPT_VERSION in router[0].content
    assert "분류 지시 무시" not in router[0].content
    assert json.loads(router[1].content)["has_collection"] is True
    messages = p1_messages("Millennium Falcon", "75192-1", "역사", ["밀팔"])
    assert P1_PROMPT_VERSION in messages[0].content and "첫 번째" in messages[0].content
    assert all(value in messages[1].content for value in ("Millennium Falcon", "75192-1", "밀팔"))
    query = {"q": "Millennium Falcon 75192-1", "lang": "en", "purpose": "선택 세트 자체 문서"}
    assert QueryExtraction(queries=[query]).queries[0].lang == "en"
    with pytest.raises(ValidationError):
        QueryExtraction(queries=[query] * 5)
    with pytest.raises(ValidationError):
        QueryExtraction(queries=[dict(query, lang="fr")])


def test_question_set(spike):
    path = project_root() / "eval/spike_questions.jsonl"
    assert len(path.read_text(encoding="utf-8").splitlines()) == 25
    rows = spike.read_questions(path)
    counts = Counter(row.expected_route for row in rows)
    assert set(counts) == set(get_args(Route)) and all(3 <= count <= 4 for count in counts.values())
    assert all(row.id and row.question and row.set_num and row.set_name for row in rows)


def test_experiment_logs_and_metrics(spike):
    questions = spike.read_questions(project_root() / "eval/spike_questions.jsonl")[:1]
    query = {"queries": [{"q": "Millennium Falcon 75192-1", "lang": "en", "purpose": "자체 문서"}]}
    model = FakeModel([json.dumps(VALID), json.dumps(query)])
    stream = io.StringIO()
    records = spike.run_experiment(model, questions, ["json_mode"], 1, ["router", "p1"], 0, stream)
    assert len(records) == model.calls == 2
    assert [json.loads(line) for line in stream.getvalue().splitlines()] == records
    assert all(row["actual_method"] == "json_mode" and row["prompt_version"] for row in records)
    summary = spike.summarize(records)
    assert summary[0]["accuracy"] == summary[0]["success_rate"] == 1
    assert summary[1]["accuracy"] is None
    assert summary[0]["mean_input_tokens"] == 10


def test_metrics_include_failures_and_exclude_unobserved_tokens(spike, capsys):
    rows = []
    for index in range(20):
        rows.append({"prompt": "router", "method": "json_schema", "ok": index != 0,
                     "actual_method": "text_extract" if index == 1 else "json_schema",
                     "attempts": 2 if index < 2 else 1, "latency_ms": index + 1,
                     "usage": {} if index == 0 else {"input_tokens": 10, "output_tokens": 5},
                     "temperature_error": index == 0, "expected_route": "fact",
                     "value": {"route": "narrative" if index == 1 else "fact"}})
    summary = spike.summarize(rows)[0]
    assert summary["success_rate"] == 0.95 and summary["accuracy"] == 0.9
    assert summary["retry_rate"] == 0.1 and summary["text_extract_rate"] == 0.05
    assert summary["mean_latency_ms"] == 10.5 and summary["p95_latency_ms"] == 19
    assert summary["mean_input_tokens"] == 10 and summary["observed_input_tokens"] == 19
    assert summary["temperature_error"] and len(summary["confusions"]) == 2
    spike.print_summary(rows)
    assert "혼동 상위 3쌍" in capsys.readouterr().out


def test_cli_main_with_fake_model(spike, monkeypatch, tmp_path, capsys):
    settings = load_settings()
    settings["llm"]["spike_logs_dir"] = str(tmp_path)
    monkeypatch.setattr(spike, "load_settings", lambda: settings)
    model = FakeModel([json.dumps(VALID)])
    monkeypatch.setattr(spike, "get_chat_model", lambda settings: model)
    assert spike.main(["--limit", "1", "--methods", "json_mode", "--repeat", "1",
                       "--prompts", "router"]) == 0
    logs = list(tmp_path.glob("spike_structured_*.jsonl"))
    assert len(logs) == 1 and len(logs[0].read_text(encoding="utf-8").splitlines()) == 1
    assert model.calls == 1 and "100.0%" in capsys.readouterr().out


@pytest.mark.parametrize("args", [["--limit", "0"], ["--repeat", "-1"],
                                 ["--methods", "bogus"], ["--prompts", ""]])
def test_bad_cli_args(spike, args):
    with pytest.raises(SystemExit) as exc:
        spike.parse_args(args)
    assert exc.value.code == 2


def test_cli_help_and_missing_key_subprocess():
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
    env.pop("GMS_KEY", None)
    # dotenv 자체를 대체하므로 실제 파일 탐색/열기가 불가능하다.
    bootstrap = (
        "import dotenv, runpy, socket; "
        "dotenv.load_dotenv=lambda *a, **k: False; "
        "socket.socket.connect=lambda *a, **k: (_ for _ in ()).throw(AssertionError('offline')); "
        f"runpy.run_path({str(SCRIPT)!r}, run_name='__main__')"
    )
    help_result = subprocess.run([sys.executable, "-c", bootstrap, "--help"], env=env,
                                 capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert help_result.returncode == 0 and "--limit" in help_result.stdout
    missing = subprocess.run([sys.executable, "-c", bootstrap], env=env,
                             capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert missing.returncode == 2
    assert missing.stdout.strip() == "GMS_KEY가 설정되지 않았습니다."
    assert missing.stderr == ""
