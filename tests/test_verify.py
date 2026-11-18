"""수치 검증·P3 버전·비교 스냅샷을 네트워크 없이 검증한다."""

import importlib.util
import json
import sqlite3
from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage

from rag_service.config import load_settings, project_root
from rag_service.rag.evidence import Card
from rag_service.rag.prompts import p3_context, p3_messages
from rag_service.rag.verify import UNCONFIRMED, allowed_values, split_sentences, verify_answer
from test_index_items_offline import offline


def card(text, n=1):
    return Card(n, "db", "제목 9999", "https://example.invalid/8888", "license", text)


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE sets(set_num TEXT PRIMARY KEY, name TEXT)")
    conn.executemany("INSERT INTO sets VALUES (?, 'Falcon')", [(n,) for n in (
        "75192-1", "10179-1", "4504-1", "4504-2", "75257-1", "75301-1")])
    yield conn
    conn.close()


@pytest.mark.parametrize("source,answer", [
    ("7,541", "7541개"), ("7541", "7,541개"), ("52", "52.0%"), ("52.00%", "52%"),
    ("75192-1", "75192"), ("75192", "75192-1"), ("fourteen", "14 파섹"),
    ("twenty-one", "21개"), ("one hundred and twenty-three", "123개"),
    ("two thousand five hundred", "2500개"), ("ninety", "90개"), ("zero", "0개"),
    ("-52.0", "-52%"), ("-0.0", "0%"), ("2017", "2017년"),
])
def test_normalization(db, source, answer):
    text = answer + " [근거 1]"
    result = verify_answer(text, [card(source)], "", db)
    assert result.answer == text and not result.removed and result.checked == 1


@pytest.mark.parametrize("text", [
    '“fourteen parsecs(14 파섹)”이라고 말합니다 [근거 1].',
    "2017년 UCS(75192)가 등장했습니다 [근거 1].",
    "UCS(75192, 10179 등)가 있습니다 [근거 1].",
])
def test_measured_false_positives(db, text):
    result = verify_answer(text, [card("fourteen parsecs; 2017; 75192-1; 10179-1")], "", db)
    assert not result.removed and result.answer == text


@pytest.mark.parametrize("number", ["75301-1", "75301"])
@pytest.mark.parametrize("with_context", [True, False])
def test_q3_selected_set_without_number_in_cited_wiki(db, number, with_context):
    text = (f"루크가 **Luke Skywalker’s X-Wing Fighter({number})**로 참여한 전투는 "
            "… “Battle of Yavin” … [근거 6]")
    wiki = Card(6, "wikipedia", "Battle of Yavin", "https://en.wikipedia.org/wiki/Battle_of_Yavin",
                "CC BY-SA", "Luke Skywalker participated in the Battle of Yavin in an X-wing fighter.")
    kwargs = {"context_set_nums": ["75301-1"]} if with_context else {}
    result = verify_answer(text, [wiki], "어떤 전투에 참여?", db, **kwargs)
    assert result.checked == 1
    if with_context:
        assert result.answer == text and not result.removed and not result.warnings
    else:
        assert result.answer == UNCONFIRMED and result.removed[0]["values"] == [number]


@pytest.mark.parametrize("context,number", [(["4504-1"], "4504-2"), (["4504-2"], "4504-1"),
                                         (["99999-1"], "99999-1"), (["99999-1"], "99999")])
def test_context_does_not_allow_other_variants_or_missing_sets(db, context, number):
    result = verify_answer(f"세트 {number} [근거 1]", [card("설명")], "", db, context_set_nums=context)
    assert result.answer == UNCONFIRMED and result.removed


@pytest.mark.parametrize("value", ["7,542개", "2015년", "12명", "799달러", "1개"])
def test_context_only_allows_identifiers_not_unrelated_values_or_suffix(db, value):
    result = verify_answer(f"{value}입니다 [근거 1]", [card("설명")], "", db, context_set_nums=["75301-1"])
    assert result.removed and result.answer == UNCONFIRMED


@pytest.mark.parametrize("bad", ["8,888개", "99999-1", "99999", "2018년", "4504-2", "7개", "3%"])
def test_unverified_values_are_removed(db, bad):
    text = f"틀린 값은 {bad}입니다 [근거 1].\n확인한 내용입니다 [근거 1]."
    result = verify_answer(text, [card("75192-1 7541 2017 4504-1")], "", db)
    assert result.answer == "확인한 내용입니다 [근거 1]."
    assert len(result.removed) == 1 and result.items[0]["sentence_index"] == 0
    assert result.warnings == ["검증되지 않은 수치를 포함한 문장 1개를 제외했습니다"]


def test_unknown_set_even_in_card_and_question(db):
    result = verify_answer("99999-1입니다 [근거 1]", [card("99999-1")], "99999-1", db)
    assert result.answer == UNCONFIRMED and result.removed[0]["values"] == ["99999-1"]


def test_explicit_variants_never_allow_other_suffixes(db):
    db.execute("INSERT INTO sets VALUES ('75192-2', 'Variant')")
    for source, answer in (("75192-2", "75192-1"), ("75192-1", "75192-2")):
        assert verify_answer(answer + " [근거 1]", [card(source)], "", db).removed


@pytest.mark.parametrize("text", ["10000개", "10,000개", "10000.0개", "10000 parts"])
def test_five_digit_quantities_are_not_set_ids(db, text):
    result = verify_answer(text + " [근거 1]", [card("10,000개")], "", db)
    assert not result.removed and result.checked == 1


def test_year_range_is_not_a_set_variant(db):
    result = verify_answer("2015-2020년 사이입니다 [근거 1].", [card("2015년과 2020년")], "", db)
    assert not result.removed and result.checked == 2


def test_prompt_context_resets_on_exception():
    with pytest.raises(RuntimeError):
        with p3_context(selected_set={"name": "PRIVATE_CONTEXT"}, table_titles=[], settings=load_settings()):
            raise RuntimeError
    assert "PRIVATE_CONTEXT" not in p3_messages("질문", [], history_turns=0, version="p3-v2")[-1].content


def test_all_cited_cards_but_not_uncited_or_metadata(db):
    cards = [card("2017"), card("7541", 2), card("1234", 3)]
    text = "7541개입니다 [근거 1]. 2017년입니다 [근거 2].\n1234개입니다 [근거 1]."
    result = verify_answer(text, cards, "", db)
    assert len(result.removed) == 1 and "7541" in result.answer and "2017" in result.answer
    assert "9999" not in allowed_values(cards, "") and "8888" not in allowed_values(cards, "")


def test_question_numbers_and_citation_exemptions(db):
    text = "## 12 제목\n10. 부족 60개 이하입니다 [근거 15].\n20) 단계 1과 2를 봅니다 [근거 15]."
    result = verify_answer(text, [card("본문", 15)], "부족 60개 이하", db)
    assert result.answer == text and result.checked == 1 and not result.removed


def test_unitless_single_digits_setting(db):
    text = "선택 7 [근거 1]"
    cfg = load_settings()
    cfg["verify"]["ignore_single_digit"] = False
    assert verify_answer(text, [card("내용")], "", db).checked == 0
    assert verify_answer(text, [card("내용")], "", db, settings=cfg).answer == UNCONFIRMED
    cfg["verify"]["enabled"] = False
    result = verify_answer(text, [], "", db, settings=cfg)
    assert result.answer == text and result.checked == 0


@pytest.mark.parametrize("text", ["틀린 2018년 [근거 1].", "틀린 2018년 [근거 1]. 남은 설명."])
def test_empty_or_no_remaining_citations_fallback(db, text):
    assert verify_answer(text, [card("2017")], "", db).answer == UNCONFIRMED


def test_sentences_keep_decimal_numbered_list_and_trailing_citations(db):
    text = "12. 52.0%입니다. [근거 1]\n13) 99.5%입니다. [근거 2] 다음 설명! [근거 1]"
    sentences = split_sentences(text)
    assert len(sentences) == 3 and "52.0%" in sentences[0] and "[근거 2]" in sentences[1]
    result = verify_answer(text, [card("52%"), card("다른 내용", 2)], "", db)
    assert "[근거 2]" not in result.answer and "다음 설명" in result.answer
    assert result.checked == 2 and result.items == [{"value": "99.5", "sentence_index": 1}]


def test_separate_citation_line_is_removed_with_its_sentence(db):
    result = verify_answer("2018년입니다.\n[근거 1]\n남은 설명.", [card("2017")], "", db)
    assert result.answer == UNCONFIRMED and len(result.removed) == 1
    assert "[근거 1]" in result.removed[0]["sentence"]


def test_prompt_v1_unchanged_and_v2_context_reset():
    cfg = load_settings()
    before = p3_messages("이거", [card("본문")], history_turns=0, version="p3-v1")
    cfg["verify"]["p3_max_bullets"] = 3
    with p3_context(selected_set={"set_num": "75192-1", "name": "Falcon"},
                    table_titles=["판본 비교"], settings=cfg):
        assert p3_messages("이거", [card("본문")], history_turns=0, version="p3-v1") == before
        messages = p3_messages("이거", [], history_turns=0, version="p3-v2")
    system, user = (m.content for m in messages)
    assert all(s in system for s in ("이 기체/이 세트/이거", "최대 3개", "1~2문장", "반복하지 않는다", "제안 문장", "NNNNN-N", "set_meta"))
    assert "화면에 표로 제공됨: 판본 비교" in user and "Falcon" in user
    assert "Falcon" not in p3_messages("이거", [], history_turns=0, version="p3-v2")[-1].content
    with pytest.raises(ValueError):
        p3_messages("q", [], history_turns=0, version="p3-v3")


@pytest.fixture
def comparison():
    spec = importlib.util.spec_from_file_location("compare_p3_test", project_root() / "scripts/compare_p3.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("initial_version", ["p3-v1", "p3-v2"])
def test_comparison_fake_model_schema_and_identical_cards(comparison, db, initial_version):
    model = Mock()
    model.invoke.side_effect = [AIMessage(content=text, usage_metadata={
        "input_tokens": 10, "output_tokens": 5, "total_tokens": 15}) for text in (
        "2018년입니다 [근거 1].", "2017년입니다 [근거 1].")]
    cfg = load_settings()
    cfg["retrieval"]["p3_prompt_version"] = initial_version
    result = comparison.compare_case({"id": "Q1", "question": "이거 언제?"}, [card("2017")],
                                     model=model, conn=db, settings=cfg,
                                     selected_set={"set_num": "75192-1", "name": "Falcon"},
                                     tables=[{"title": "판본 비교", "rows": []}])
    assert json.loads(json.dumps(result, ensure_ascii=False)) == result
    v1, v2 = result["versions"].values()
    assert v1["removed_count"] == 1 and v2["removed_count"] == 0
    assert v1["sentence_count"] == v2["sentence_count"] == 1
    assert v1["citation_count"] == v2["citation_count"] == 1
    assert v2["usage"]["p3"]["input_tokens"] == 10 and v2["elapsed_ms"] >= 0
    assert v1["no_citation"] and not v2["no_citation"]
    prompts = [call.args[0][-1].content.split("제공된 근거:\n")[1] for call in model.invoke.call_args_list]
    assert prompts[0] == prompts[1]
    assert cfg["retrieval"]["p3_prompt_version"] == initial_version


def test_comparison_passes_selected_set_to_verification(comparison, db):
    model = Mock()
    model.invoke.return_value = AIMessage(content="75301-1은 Battle of Yavin과 관련이 있습니다 [근거 1].")
    result = comparison.compare_case({"id": "Q3", "question": "어떤 전투에 참여?"}, [card("Battle of Yavin")],
                                     model=model, conn=db, settings=load_settings(),
                                     selected_set={"set_num": "75301-1", "name": "X-wing"})
    assert all(row["removed_count"] == 0 and row["checked"] == 1 for row in result["versions"].values())


def test_comparison_help_and_questions(comparison, capsys):
    with pytest.raises(SystemExit) as exc:
        comparison.main(["--help"])
    assert exc.value.code == 0 and "--prepared" in capsys.readouterr().out
    cases = [json.loads(line) for line in (project_root() / "eval/p3_questions.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(cases) == len({case["id"] for case in cases}) == 13
    assert cases[9]["history"] and cases[-1]["collection_sets"]


def test_preparation_retrieves_once_and_keeps_original_question(comparison, db, monkeypatch):
    from rag_service.rag import retrieve
    from rag_service.rag.graph import RagDeps
    from rag_service.services.orchestrator import OrchestratorDeps

    mocked = Mock(return_value={"cards": [card("2017")]})
    monkeypatch.setattr(retrieve, "retrieve", mocked)
    case = {"id": "Q1", "question": "이 기체는 누가 디자인했어?", "selected_set": "75192-1"}
    cards, selected, tables = comparison.prepare_case(case, rag_deps=RagDeps(), orchestrator_deps=OrchestratorDeps(db))
    mocked.assert_called_once()
    assert mocked.call_args.args[0]["question"] == case["question"]
    assert cards[0].text == "2017" and selected["set_num"] == "75192-1" and tables == []


def test_comparison_prepared_cli_writes_json_without_retrieval(comparison, db, monkeypatch, tmp_path):
    from contextlib import contextmanager
    from dataclasses import asdict
    from rag_service import llm
    from rag_service.rag import graph
    from rag_service.services import orchestrator

    case = {"id": "Q1", "question": "출시?", "history": []}
    snapshot = {**case, "cards": [asdict(card("2017"))], "selected_set": {"set_num": "75192-1"}, "tables": []}
    questions, prepared, output = (tmp_path / name for name in ("q.jsonl", "snapshot.json", "result.json"))
    questions.write_text(json.dumps(case), encoding="utf-8")
    prepared.write_text(json.dumps({"results": [snapshot]}), encoding="utf-8")
    model = Mock()
    model.invoke.return_value = AIMessage(content="2017년입니다 [근거 1].")
    monkeypatch.setattr(llm, "get_chat_model", lambda settings: model)
    forbidden = Mock(side_effect=AssertionError("retrieval factory must not run"))
    monkeypatch.setattr(graph, "default_deps", forbidden)

    @contextmanager
    def dependencies():
        yield orchestrator.OrchestratorDeps(db)

    monkeypatch.setattr(orchestrator, "default_deps", dependencies)
    assert comparison.main(["--questions", str(questions), "--prepared", str(prepared), "--output", str(output)]) == 0
    records = json.loads(output.read_text(encoding="utf-8"))["results"]
    assert set(records[0]["versions"]) == {"p3-v1", "p3-v2"} and model.invoke.call_count == 2
    forbidden.assert_not_called()
