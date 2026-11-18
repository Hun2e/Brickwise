"""T-018: 숫자 문맥, A 격리, 운영 B 연결, 산출물과 안전한 오류 처리."""

import importlib.util
import json
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage

from rag_service.config import load_settings, project_root
from rag_service.eval.common import digest
from rag_service.eval.rag_compare import check_facts, run_case, says_unknown, write_reports
from rag_service.eval.schema import read_cases
from rag_service.rag.evidence import Card
from rag_service.rag.prompts import SOLO_BASELINE
from rag_service.services.orchestrator import OrchestratorResponse


@pytest.fixture
def settings(tmp_path):
    result = load_settings()
    result["llm"]["cache_enabled"] = False
    result["evaluation"]["results_dir"] = str(tmp_path / "results")
    result["router"]["error_log"] = str(tmp_path / "errors.log")
    return result


@pytest.fixture
def cli():
    spec = importlib.util.spec_from_file_location("compare_rag_cli", project_root() / "scripts/compare_rag.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def case():
    return {"id": "B-01", "set_num": "75192-1", "question": "75192 부품 수는?", "type": "fact",
            "facts": [{"field": "num_parts", "value": 7541}], "note": "테스트"}


@pytest.mark.parametrize("answer,status,found", [
    ("부품은 7,541개입니다.", "correct", ["7541"]),
    ("부품: **7541 개** [근거 1]", "correct", ["7541"]),
    ("총 7,541개입니다.", "correct", ["7541"]),
    ("7,541 피스입니다.", "correct", ["7541"]),
    ("부품 수는 7541.0개입니다.", "correct", ["7541"]),
    ("부품 7,500개", "wrong", ["7500"]),
    ("부품 7,541개 또는 7,500개", "wrong", ["7541", "7500"]),
    ("확인되지 않습니다", "missing", []),
    ("75192-1은 2017년 출시, 미니피그 8개 [근거 7541]", "missing", []),
    ("부품 17,541개입니다", "wrong", ["17541"]),
    ("가격은 7,541원입니다", "missing", []),
])
def test_numbers(answer, status, found):
    assert check_facts(answer, case()["facts"]) == [
        {"field": "num_parts", "expected": 7541, "status": status, "found": found}]


def test_mixed_fields_and_zero_minifigs():
    facts = [{"field": "year", "value": 2026}, {"field": "num_parts", "value": 581},
             {"field": "minifigs", "value": 0}]
    answer = "75423-1은 2026년 출시, 부품 581개, 미니피규어 0개입니다. [근거 1]"
    assert [r["status"] for r in check_facts(answer, facts)] == ["correct"] * 3
    assert check_facts("미니피그 7개", [{"field": "minifigs", "value": 0}])[0]["status"] == "wrong"
    assert check_facts("미니피그가 없습니다", [{"field": "minifigs", "value": 0}])[0]["status"] == "missing"


@pytest.mark.parametrize("answer", [
    "75192-1: 부품 7,541개, 2017년. 75257-1: 부품 1,328개, 2019년.",
    "## 75192\n- 부품 7,541개\n- 출시 2017년\n## 75257\n- 부품 1,328개\n- 출시 2019년",
    "| 세트 | 부품 수 | 연도 |\n|---|---:|---|\n|75192|7,541|2017|\n|75257|1,328|2019|",
    "| 항목 | 75192 | 75257 |\n|---|---|---|\n|부품 수|7,541|1,328|\n|연도|2017|2019|",
])
def test_comparison_prose_headings_and_tables(answer):
    facts = read_cases(project_root() / "eval/rag_compare.jsonl", "rag_compare")[3]["facts"]
    assert [r["status"] for r in check_facts(answer, facts)] == ["correct"] * 4


def test_swapped_sets_and_variants_are_not_correct():
    facts = [{"field": "num_parts:75192-1", "value": 7541},
             {"field": "num_parts:75257-1", "value": 1328}]
    assert [r["status"] for r in check_facts("75192 부품 1328개, 75257 부품 7541개", facts)] == ["wrong"] * 2
    assert check_facts("75192-2 부품 7541개", facts)[0]["status"] == "missing"
    assert all(r["status"] == "missing" for r in check_facts("부품 7541개와 1328개", facts))
    assert all(r["status"] == "missing" for r in check_facts("75192와 75257은 각각 7541개, 1328개", facts))


@pytest.mark.parametrize("fact", [{"field": "price", "value": 12}, {"field": "year:x", "value": 2020},
                                 {"field": "year", "value": True}, {"field": "year", "value": None},
                                 {"field": "num_parts", "value": -1}, {"field": "year", "value": "NaN"}])
def test_invalid_facts_rejected(fact):
    with pytest.raises(ValueError):
        check_facts("", [fact])


@pytest.mark.parametrize("answer", ["제공된 자료에서 확인되지 않습니다", "확인되지 않음", "모르겠습니다",
                                     "모릅니다", "모르겠다", "정보가 없습니다", "확인할 수 없습니다", "알 수 없음"])
def test_unknown(answer):
    assert says_unknown(answer)


@pytest.mark.parametrize("answer", ["부품은 581개입니다", "출시 연도는 확인됩니다", ""])
def test_unknown_false(answer):
    assert not says_unknown(answer)


def test_unknown_is_presence_detection_not_semantic_grading():
    assert says_unknown("모르는 것이 아닙니다")


def test_run_case_solo_isolated_usage_and_actual_citations(settings):
    events = []

    def invoke(messages):
        events.append("solo")
        assert [(m.type, m.content) for m in messages] == [("system", SOLO_BASELINE), ("human", case()["question"])]
        return AIMessage(content="부품 7,541개", usage_metadata={"input_tokens": 12, "output_tokens": 9, "total_tokens": 21})

    cards = [Card(n, "db", f"세트 {n}", f"https://example.test/{n}", "CC0", "7541개") for n in (2, 4)]

    def service(question, *, selected_set):
        assert events == ["solo"]
        events.append("service")
        assert (question, selected_set) == (case()["question"], case()["set_num"])
        return OrchestratorResponse(answer="부품 7541개 [근거 2] [근거 2, 99]", cards=cards,
                                    usage={"router": {"input_tokens": 10}}, warnings=["경고"])

    result = run_case(case(), solo_model=SimpleNamespace(invoke=invoke), answer_fn=service, settings=settings)
    assert events == ["solo", "service"]
    assert result["A"]["usage"] == {"calls": 1, "input_tokens": 12, "output_tokens": 9}
    assert result["B"]["citation_count"] == 2
    assert result["B"]["citation_mentions"] == 2
    assert result["B"]["cited_card_count"] == 1
    assert result["B"]["cards"][0]["n"] == 2
    assert len(result["B"]["warnings"]) == 2
    assert result["B"]["usage"] == {"router": {"input_tokens": 10}}
    assert result["settings_hash"] == digest(settings)


def test_legacy_usage_content_blocks_and_no_token_invention(settings):
    model = Mock()
    model.invoke.return_value = SimpleNamespace(content=[{"type": "text", "text": "부품 7541개"}],
                                                 response_metadata={"token_usage": {"prompt_tokens": 3}})
    result = run_case(case(), solo_model=model, answer_fn=lambda *a, **kw: OrchestratorResponse(), settings=settings)
    assert result["A"]["usage"] == {"calls": 1, "input_tokens": 3}


def test_invalid_conditions_fail_before_calls(settings):
    settings["llm"]["cache_enabled"] = True
    model, service = Mock(), Mock()
    with pytest.raises(ValueError):
        run_case(case(), solo_model=model, answer_fn=service, settings=settings)
    model.invoke.assert_not_called()
    service.assert_not_called()


def _mock_cli(monkeypatch, settings):
    import rag_service.config as config
    import rag_service.eval.runtime as runtime
    import rag_service.llm as llm
    import rag_service.services.orchestrator as orchestrator

    original = deepcopy(settings)
    original["llm"]["cache_enabled"] = True
    monkeypatch.setattr(config, "load_settings", lambda: original)
    events, passed_settings = [], []

    def invoke(messages):
        events.append("A")
        assert len(messages) == 2 and messages[0].content == SOLO_BASELINE
        return "확인되지 않습니다"

    model = SimpleNamespace(invoke=invoke, cache=True)

    def factory(config):
        passed_settings.append(config)
        assert config["llm"]["cache_enabled"] is False
        return model

    @contextmanager
    def dependencies(config, *, live):
        assert events == ["A"] and live is True
        assert config["retrieval"] == original["retrieval"]
        assert config["collection"] == original["collection"]
        assert config["paths"] == original["paths"]
        events.append("open")
        yield SimpleNamespace(model=SimpleNamespace(cache=True), settings=config)
        events.append("close")

    def answer(question, *, selected_set, deps):
        assert events[-1] in {"A", "open"}
        assert deps.model.cache is False
        events.append("B")
        return OrchestratorResponse(answer="확인되지 않습니다", set_nums=[selected_set])

    monkeypatch.setattr(llm, "get_chat_model", factory)
    monkeypatch.setattr(runtime, "dependencies", dependencies)
    monkeypatch.setattr(orchestrator, "answer", answer)
    return events, original, model


def test_cli_all_eight_cases_and_three_reports(cli, settings, monkeypatch):
    events, original, model = _mock_cli(monkeypatch, settings)
    source = project_root() / "eval/rag_compare.jsonl"
    before = source.read_bytes()
    assert cli.main(["--file", str(source), "--name", "rag-compare"]) == 0
    assert source.read_bytes() == before
    assert events.count("A") == events.count("B") == 8
    assert events[-1] == "close" and model.cache is False
    assert original["llm"]["cache_enabled"] is True
    paths = list(Path(settings["evaluation"]["results_dir"]).iterdir())
    assert len(paths) == 3
    data = json.loads(next(p for p in paths if p.suffix == ".json").read_text(encoding="utf-8"))
    assert len(data["rows"]) == 8
    assert data["settings_hash"] == digest(settings)
    assert data["collection_cache_preserved"] is True
    assert data["solo_prompt_version"] == "solo-v1"
    sheet = next(p for p in paths if p.name.endswith("-scoring.md")).read_text(encoding="utf-8")
    assert sheet.count("| 사실 정확성 |  |  |") == 16
    assert sheet.count("| 근거 제시 |  |  |") == 16
    assert sheet.count("| 모름 처리 |  |  |") == 16
    assert "공식 1,351 / DB 기준 1,328" in sheet
    events.clear()
    assert cli.main(["--file", str(source), "--name", "rag-compare"]) == 1
    assert events == []


def test_cli_missing_key_no_db_or_model_calls(cli, settings, monkeypatch, capsys):
    import rag_service.config as config
    import rag_service.llm as llm
    import rag_service.eval.runtime as runtime

    monkeypatch.setattr(config, "load_settings", lambda: settings)
    monkeypatch.setattr(llm, "_load_environment", lambda: None)
    monkeypatch.delenv("GMS_KEY", raising=False)
    resources = Mock(side_effect=AssertionError("must not open DB"))
    monkeypatch.setattr(runtime, "dependencies", resources)
    assert cli.main(["--file", "eval/rag_compare.jsonl", "--name", "missing"]) == 1
    output = capsys.readouterr()
    assert not output.out and len(output.err.splitlines()) == 1
    assert "GMS_KEY" in output.err and "Traceback" not in output.err
    resources.assert_not_called()
    assert not Path(settings["evaluation"]["results_dir"]).exists()


def test_cli_exception_only_in_file_log(cli, settings, monkeypatch, capsys):
    _mock_cli(monkeypatch, settings)
    assert cli.main(["--file", "nonexistent-t018.jsonl", "--name", "error"]) == 1
    output = capsys.readouterr()
    assert len(output.err.splitlines()) == 1 and "nonexistent-t018" not in output.err
    log = Path(settings["router"]["error_log"]).read_text(encoding="utf-8")
    assert "Traceback" in log and "nonexistent-t018" in log


def test_cli_service_failure_preserved(cli, settings, monkeypatch):
    import rag_service.services.orchestrator as orchestrator

    _mock_cli(monkeypatch, settings)
    monkeypatch.setattr(orchestrator, "answer", lambda *a, **kw: OrchestratorResponse(
        answer="데이터를 확인하세요", needs_input="데이터를 확인하세요", warnings=["upstream_unavailable"]))
    assert cli.main(["--file", "eval/rag_compare.jsonl", "--name", "failure"]) == 1
    path = next(Path(settings["evaluation"]["results_dir"]).glob("*.json"))
    assert all(row["B"]["needs_input"] for row in json.loads(path.read_text(encoding="utf-8"))["rows"])


def test_reports_preserve_full_text_and_protect_scoring_sheet(settings):
    answer = "첫 줄 | 값\n\n## 답변 내부 제목\n끝"
    row = run_case(case(), solo_model=SimpleNamespace(invoke=lambda _: answer),
                   answer_fn=lambda *a, **kw: OrchestratorResponse(answer=answer), settings=settings)
    now = datetime.now(timezone.utc)
    paths = write_reports([row], settings=settings, name="review", started_at=now, dataset_hash="dataset")
    assert json.loads(paths[0].read_text(encoding="utf-8"))["rows"][0]["A"]["answer"] == answer
    assert "> ## 답변 내부 제목" in paths[2].read_text(encoding="utf-8")
    paths[0].unlink()
    paths[1].unlink()
    with pytest.raises(ValueError):
        write_reports([row], settings=settings, name="review", started_at=now, dataset_hash="dataset")


def test_help_without_configuration(cli, monkeypatch, capsys):
    import rag_service.config as config

    monkeypatch.setattr(config, "load_settings", Mock(side_effect=AssertionError("no setup")))
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    assert exc.value.code == 0 and "--file" in capsys.readouterr().out
