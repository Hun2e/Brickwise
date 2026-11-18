"""라우터·7경로·선택·공통 응답을 외부 호출 없이 검증한다."""

import importlib.util
import json
from contextlib import contextmanager
from collections import Counter
from dataclasses import asdict
from types import SimpleNamespace
from typing import get_args
from unittest.mock import Mock

import pytest
import httpx
from openai import APIConnectionError, APIStatusError, APITimeoutError, InternalServerError, RateLimitError
from langchain_core.embeddings import DeterministicFakeEmbedding
from langchain_core.messages import AIMessage
from pydantic import ValidationError

from rag_service.config import load_settings, project_root
from rag_service.llm import MissingApiKey
from rag_service.data_prep.collectors.http import UpstreamError
from rag_service.planner.recommend import Recommendation
from rag_service.planner.sets import SetInfo
from rag_service.rag.evidence import Card
from rag_service.rag.graph import RagDeps
from rag_service.rag.prompts import router_messages, selection_messages
from rag_service.rag.schemas import Route, RouterOutput, RouterOutputV2, SelectionCriteria
from rag_service.rag.templates import TemplateAnswer
from rag_service.services.orchestrator import OrchestratorDeps, OrchestratorResponse, answer, select_recommendation
from rag_service.services import orchestrator
from rag_service.rag.citations import NO_CITATION_MESSAGE
from test_db_queries import add_set, conn
from test_index_items_offline import offline
from retrieval_helpers import configure_similarity
from test_structured_offline import FakeModel


@pytest.fixture
def deps(conn, tmp_path):
    settings = load_settings()
    settings["paths"].update(docs_dir=str(tmp_path / "docs"), collect_sqlite=str(tmp_path / "cache.sqlite"))
    settings["paths"]["chroma_dir"] = str(tmp_path / "chroma")
    settings["llm"]["max_retries"] = 0
    settings["router"]["error_log"] = str(tmp_path / "logs/orchestrator.log")
    settings["verify"]["log_path"] = str(tmp_path / "logs/verification.jsonl")
    return OrchestratorDeps(conn, settings)


def model(route="fact", *, numbers=(), refs=(), confidence=0.95, extra=()):
    return FakeModel([json.dumps({"route": route, "set_nums": list(numbers), "set_refs": list(refs),
                                  "confidence": confidence, "entities": []}),
                      *(json.dumps(item) if isinstance(item, dict) else item for item in extra)])


def card(n=7, text="DB 값"):
    return Card(n, "db", "제목", "https://rebrickable.com/", "Rebrickable", text)


def template(n=7):
    return TemplateAnswer(f"결과 [근거 {n}]", [card(n), card(99)],
                          [{"title": "표", "columns": ["수량"], "rows": [[3]], "formats": {"수량": "count"}}], [])


def fake_ask(number, question, **kwargs):
    callback = kwargs.get("on_event")
    if callback:
        for name in ("collecting", "retrieving", "generating", "done"):
            callback(name, {})
    cards = kwargs["injected_cards"] or [card()]
    return {"answer": f"설명 [근거 {cards[-1].n}]", "cards": cards,
            "usage": {"p3": {"calls": 1}}, "timings": {"generate_ms": 1}, "warnings": []}


def test_v2_context_examples_and_v1_unchanged():
    v1 = router_messages("질문", False)
    assert v1 == router_messages("질문", False, "router-v1", selected_set={"set_num": "A-1"})
    assert "set_refs" not in RouterOutput.model_fields
    history = [{"question": "old", "answer": "old"},
               {"question": "prior", "route": "fact", "answer": "가" * 250}]
    messages = router_messages("그거 언제 나왔어?", True, "router-v2",
                               selected_set={"set_num": "A-1", "name": "Falcon"}, history=history)
    data = json.loads(messages[1].content)
    assert data["selected_set"]["name"] == "Falcon" and data["has_collection"]
    assert data["previous_turn"] == {"question": "prior", "route": "fact", "answer": "가" * 200}
    assert all(key in messages[0].content for key in RouterOutputV2.model_fields)
    assert all(word in messages[0].content for word in ("연도별로", "그거 언제", '"route":"fact"', '"route":"compare"'))
    assert "prior" not in messages[0].content
    assert json.loads(router_messages("q", False, "router-v2")[1].content)["selected_set"] == "없음"


def test_message_history_and_json_selection_keys():
    messages = router_messages("질문", False, "router-v2", history=[
        {"role": "user", "content": "질문1"}, {"role": "assistant", "content": "답변1", "route": "fact"}])
    assert json.loads(messages[1].content)["previous_turn"]["route"] == "fact"
    selection = selection_messages("조건")
    assert all(key in selection[0].content for key in SelectionCriteria.model_fields)
    assert '"max_missing":20' in selection[0].content and '"max_missing":19' in selection[0].content
    with pytest.raises(ValueError):
        router_messages("q", False, "bad")
    with pytest.raises(ValueError):
        selection_messages("q", "bad")


@pytest.mark.parametrize("has_collection", [False, True])
@pytest.mark.parametrize("question", ["이 세트 부품 수와 영화 속 역할을 함께 알려줘", "이 기체의 출시 연도와 배경을 설명해줘"])
def test_attributes_with_story_do_not_require_owned_parts(deps, has_collection, question):
    deps.model = model("build_archive", refs=["selected"])
    deps.collection = {"sets": [("A-1", 1)]} if has_collection else {}
    deps.ask_fn = Mock(side_effect=fake_ask)
    deps.recommend_fn = Mock()
    result = answer(question, selected_set="A-1", deps=deps)
    assert result.route == "narrative" and result.route_source == "rule:attributes_with_story"
    assert result.needs_input is None
    deps.ask_fn.assert_called_once()
    deps.recommend_fn.assert_not_called()


def test_owned_parts_with_story_still_requires_collection(deps):
    deps.model = model("build_archive", refs=["selected"])
    result = answer("내 부품으로 이 세트를 만들 수 있는지와 영화 속 역할을 알려줘", selected_set="A-1", deps=deps)
    assert result.route == "build_archive" and result.route_source == "rule:no_collection"
    assert result.needs_input


@pytest.mark.parametrize("data", [{"sort": "random"}, {"order": "up"}, {"max_missing": -1},
                                  {"max_missing": True}, {"max_missing": 2.5}])
def test_selection_schema_validation(data):
    with pytest.raises(ValidationError):
        SelectionCriteria(**data)


@pytest.mark.parametrize("route", ["fact", "set_ops", "build", "filter", "narrative", "compare", "build_archive"])
def test_seven_routes_and_contract(deps, route):
    numbers = ["A", "B"] if route == "set_ops" else ["A"]
    deps.model = model(route, numbers=numbers, extra=[{}])
    deps.collection = {"sets": [("A-1", 1)]}
    deps.ask_fn = Mock(side_effect=fake_ask)
    deps.fact_fn = Mock(return_value=template())
    deps.set_ops_fn = Mock(return_value=template())
    deps.analysis_fn = Mock(return_value=template())
    compared = template()
    compared.cards = compared.cards[:1]
    deps.compare_fn = Mock(return_value=compared)
    deps.search_fn = Mock(return_value=SimpleNamespace(template=template(), set_nums=["B-1"], warnings=[],
                                                      usage={"filter": {"calls": 1}}, elapsed_ms=4))
    rec = Recommendation(SetInfo("B-1", "B", 2018, 5, "Star Wars", "core"), 1, 1, .8, 5, False)
    deps.recommend_fn = Mock(return_value=[rec])
    events = []
    question = "부품 몇 개?" if route == "fact" else "특징을 알려줘"
    result = answer(question, deps=deps, on_event=lambda n, d: events.append(n))
    assert isinstance(result, OrchestratorResponse) and result.route == route and result.route_source == "llm"
    assert result.needs_input is None, result
    assert set(asdict(result)) == {"route", "route_source", "answer", "cards", "tables", "set_nums",
                                   "needs_input", "warnings", "usage", "timings", "verification",
                                   "items", "state_delta", "spec"}
    assert events[:2] == ["routing", "resolving"] and events[-1] == "done" and events.count("done") == 1
    assert "total_ms" in result.timings and "router" in result.usage
    if route in ("narrative", "compare", "build_archive"):
        deps.ask_fn.assert_called_once()
        assert bool(deps.ask_fn.call_args.kwargs["injected_cards"]) == (route != "narrative")
        assert result.cards[0].n == (2 if route == "build_archive" else 1 if route == "compare" else 7)
        if route == "build_archive":
            assert result.set_nums == ["B-1"] and result.cards[0].title == "선택 이유"
            assert deps.recommend_fn.call_args.kwargs["top_n"] == deps.settings["router"]["build_archive_pool"]
        if route == "compare":
            deps.compare_fn.assert_called_once_with(deps.conn, "A-1")
            assert result.tables[0]["formats"] == {"수량": "count"}
    else:
        getattr(deps, {"fact": "fact_fn", "set_ops": "set_ops_fn", "build": "analysis_fn", "filter": "search_fn"}[route]).assert_called_once()
        assert [c.n for c in result.cards] == [7]


@pytest.mark.parametrize("initial,question,numbers,confidence,collection,rule,final", [
    ("build", "만들기", [], .1, False, "no_collection", "build"),
    ("build_archive", "만들기 특징", [], .9, False, "no_collection", "build_archive"),
    ("build_archive", "제작 추천", [], .1, True, "low_confidence", "narrative"),
    ("narrative", "연도별 차이", ["A"], .1, False, "compare_cue", "compare"),
    ("narrative", "공통 부품", ["A", "B"], .1, False, "two_sets_parts", "set_ops"),
    ("fact", "부품 몇 개", ["A"], .5, False, "low_confidence", "narrative"),
])
def test_five_safety_rules(deps, initial, question, numbers, confidence, collection, rule, final):
    deps.model = model(initial, numbers=numbers, confidence=confidence)
    deps.collection = {"sets": [("A-1", 1)]} if collection else None
    deps.ask_fn = Mock(side_effect=fake_ask)
    result = answer(question, deps=deps)
    assert (result.route, result.route_source) == (final, "rule:" + rule)
    assert bool(result.needs_input) == (rule == "no_collection" or not numbers)


@pytest.mark.parametrize("initial", ["fact", "narrative"])
@pytest.mark.parametrize("question", [
    "루크가 들어 있는 엑스윙 찾아줘",
    "엑스윙 세트 찾아줘",
    "루크 미니피그가 들어 있는 X-wing 세트 찾아줘",
])
def test_search_cue_dispatches_misclassified_questions(deps, initial, question):
    deps.model = model(initial)
    deps.search_fn = Mock(return_value=SimpleNamespace(
        template=template(), set_nums=["B-1"], warnings=[], usage={}, elapsed_ms=0))
    deps.ask_fn = Mock()
    deps.fact_fn = Mock()
    result = answer(question, deps=deps)
    assert (result.route, result.route_source) == ("filter", "rule:search_cue")
    assert result.needs_input is None
    assert result.set_nums == ["B-1"]
    assert deps.search_fn.call_args.args[0] == question
    deps.search_fn.assert_called_once()
    deps.ask_fn.assert_not_called()
    deps.fact_fn.assert_not_called()


@pytest.mark.parametrize("question,initial,numbers,refs,expected", [
    ("75301 찾아줘", "narrative", [], [], "narrative"),
    ("75301-1 찾아줘", "narrative", [], [], "narrative"),
    ("A-1 찾아줘", "narrative", [], [], "narrative"),
    ("피규어 누구누구 들어있어?", "fact", [], [], "fact"),
    ("들어 있는 피규어 누구야?", "fact", [], [], "fact"),
    ("들어있는 피규어 누구야?", "fact", [], [], "fact"),
    ("그거 보여줘", "narrative", [], [], "narrative"),
    ("이 세트 보여줘", "narrative", [], [], "narrative"),
    ("그세트 보여줘", "narrative", [], [], "narrative"),
    ("이거 보여줘", "narrative", [], [], "narrative"),
    ("피규어 보여줘", "fact", [], ["selected"], "fact"),
    ("피규어 보여줘", "fact", ["A-1"], [], "fact"),
    ("내 부품으로 뭐 만들 수 있어?", "build", [], [], "build"),
    ("내 부품으로 만들 세트 찾아줘", "build", [], [], "build"),
    ("추천 세트 찾아줘", "build_archive", [], [], "build_archive"),
])
def test_search_cue_preserves_targets_and_other_routes(deps, question, initial, numbers, refs, expected):
    out = RouterOutputV2(route=initial, confidence=.95, set_nums=numbers, set_refs=refs)
    route, source, _ = orchestrator._rules(out, question, "A-1", True, deps, "llm")
    assert route == expected
    assert source != "rule:search_cue"


@pytest.mark.parametrize("question", [
    "엑스윙 검색", "엑스윙 보여줘", "알려줘 세트", "어떤 세트에 루크가 있니?",
    "엑스윙 세트 목록", "루크가 있는 세트", "루크가 들어 있는 세트",
    "루크가 들어있는 세트", "들어 있는 피규어 목록", "들어있는 피규어 목록",
    "2021년 세트 찾아줘", "부품 1000개 세트 찾아줘", "연도별 세트 비교 목록 보여줘",
])
def test_search_cues_precede_attribute_compare_and_confidence_rules(deps, question):
    out = RouterOutputV2(route="fact", confidence=.1)
    route, source, resolved = orchestrator._rules(out, question, None, False, deps, "llm")
    assert (route, source) == ("filter", "rule:search_cue")
    assert resolved.needs_input is None


def test_search_cues_are_configured_and_unsupported_attributes_take_priority(deps):
    out = RouterOutputV2(route="narrative", confidence=.95)
    route, source, _ = orchestrator._rules(out, "가격 알려줘 세트", None, False, deps, "llm")
    assert (route, source) == ("fact", "rule:unsupported_attr")
    deps.settings["router"]["search_cues"] = []
    assert orchestrator._rules(out, "엑스윙 찾아줘", None, False, deps, "llm")[:2] == ("narrative", "llm")


def test_compare_rule_requires_verified_subject(deps):
    deps.model = model("narrative")
    deps.ask_fn = Mock()
    result = answer("연도별로 달라졌어?", deps=deps)
    assert result.route == "narrative" and result.needs_input
    deps.ask_fn.assert_not_called()


def test_build_recommendation_without_target(deps):
    deps.model = model("build")
    deps.collection = {"sets": [("A-1", 1)]}
    deps.build_fn = Mock(return_value=template())
    deps.analysis_fn = Mock()
    result = answer("만들 수 있는 것 추천", deps=deps)
    assert not result.needs_input
    deps.build_fn.assert_called_once_with(deps.conn, deps.collection, "exact", {})
    deps.analysis_fn.assert_not_called()


def test_context_cache_differentiates_selected_history_and_collection(deps):
    deps.model = FakeModel([json.dumps({"route": "fact", "confidence": .9})] * 8)
    assert answer("출시?", selected_set="A", deps=deps).route == "fact"
    cached = answer("출시?", selected_set="A", deps=deps)
    assert deps.model.calls == 1 and cached.usage["router"] == {"cached": True}
    answer("출시?", selected_set="B", deps=deps)
    answer("출시?", selected_set="A", history=[{"question": "q", "route": "fact", "answer": "a"}], deps=deps)
    deps.collection = {"sets": [("B-1", 1)]}
    answer("출시?", selected_set="A", deps=deps)
    deps.settings["llm"]["chat_model"] = "other-model"
    answer("출시?", selected_set="A", deps=deps)
    assert deps.model.calls == 5


def test_cache_bad_payload_retries_and_failures_not_cached(deps):
    deps.cache = Mock()
    deps.cache.get_llm_cache.return_value = '{"route":"invalid"}'
    deps.model = model("fact", numbers=["A"])
    assert answer("부품 몇 개", deps=deps).route == "fact"
    deps.cache.save_llm_cache.assert_called_once()
    deps.cache.reset_mock()
    deps.model = FakeModel(["bad json", "bad json"])
    deps.ask_fn = Mock(side_effect=fake_ask)
    for _ in range(2):
        result = answer("다른 질문", selected_set="A", deps=deps)
        assert result.route_source == "fallback" and result.route == "narrative"
    deps.cache.save_llm_cache.assert_not_called()


def test_unknown_number_and_invalid_selected_return_candidates(deps):
    add_set(deps.conn, "12345-1")
    deps.model = model(numbers=["1234"])
    deps.fact_fn = Mock()
    result = answer("정보", deps=deps)
    assert result.needs_input and result.tables[0]["rows"][0][0] == "12345-1"
    deps.fact_fn.assert_not_called()
    result = answer("정보", selected_set="1234", deps=deps)
    assert result.needs_input and result.tables


def test_missing_key_and_internal_errors_are_safe(deps):
    deps.model = Mock()
    deps.model.with_structured_output.side_effect = MissingApiKey("SECRET PROMPT TRACEBACK")
    result = answer("정보", deps=deps)
    assert result.needs_input and "API 키가 설정되지 않았습니다" in result.answer
    assert "SECRET" not in json.dumps(asdict(result))
    deps.model = model(numbers=["A"])
    deps.fact_fn = Mock(side_effect=ValueError("SECRET PROMPT TRACEBACK"))
    result = answer("부품 몇 개", deps=deps)
    assert result.needs_input and "SECRET" not in json.dumps(asdict(result))


def test_empty_and_unknown_collections_need_input(deps):
    deps.model = model("build")
    deps.collection_store = Mock()
    deps.collection_store.get.side_effect = KeyError("private")
    result = answer("제작", collection_id="missing", deps=deps)
    assert result.route_source == "rule:no_collection" and result.needs_input
    assert answer(" ", deps=deps).needs_input


@pytest.fixture
def recs():
    return [Recommendation(SetInfo("100-1", "new", 2020, 100, "SW", "core"), 0, 0, 1., 100, False),
            Recommendation(SetInfo("101-1", "old", 2015, 200, "SW", "core"), 20, 1, .9, 200, False),
            Recommendation(SetInfo("102-1", "older", 2010, 300, "SW", "core"), 21, 1, .93, 300, False)]


@pytest.mark.parametrize("criteria,expected", [({}, "100-1"), ({"sort": "year"}, "102-1"),
    ({"sort": "year", "max_missing": 20}, "101-1"), ({"sort": "year", "max_missing": 19}, "100-1"),
    ({"sort": "coverage", "order": "desc"}, "100-1"), ({"sort": "num_parts", "order": "desc"}, "102-1")])
def test_deterministic_selection(recs, criteria, expected):
    assert select_recommendation(recs, SelectionCriteria(**criteria)).set_info.set_num == expected
    assert select_recommendation([], SelectionCriteria()) is None


@pytest.mark.parametrize("criteria,expected", [({"sort": "year", "max_missing": 20}, "101-1"), ({}, "100-1")])
def test_archive_selection_and_injected_reason(deps, recs, criteria, expected):
    deps.model = model("build_archive", extra=[criteria])
    deps.collection = {"sets": [("A-1", 1)]}
    deps.recommend_fn = Mock(return_value=recs)
    deps.ask_fn = Mock(side_effect=fake_ask)
    result = answer("내 세트로 만들 것의 특징", deps=deps)
    assert result.set_nums == [expected]
    assert deps.ask_fn.call_args.args[0] == expected
    injected = deps.ask_fn.call_args.kwargs["injected_cards"]
    assert len(injected) == 2 and injected[1].title == "선택 이유" and expected in injected[1].text
    if criteria:
        assert "부족 20개 이하" in injected[1].text and "가장 오래된" in injected[1].text


def test_archive_no_matches_does_not_generate(deps, recs):
    deps.model = model("build_archive", extra=[{"max_missing": 0}])
    deps.collection = {"sets": [("A-1", 1)]}
    deps.recommend_fn = Mock(return_value=recs[1:])
    deps.ask_fn = Mock()
    result = answer("만들 것의 특징", deps=deps)
    assert not result.set_nums and "결과가 없습니다" in result.answer
    deps.ask_fn.assert_not_called()


def test_archive_parse_failure_uses_default_and_warns(deps, recs):
    deps.model = model("build_archive", extra=["not JSON"])
    deps.collection = {"sets": [("A-1", 1)]}
    deps.recommend_fn = Mock(return_value=recs)
    deps.ask_fn = Mock(side_effect=fake_ask)
    result = answer("만들 것의 특징", deps=deps)
    assert result.set_nums == ["100-1"] and any("기준 추출에 실패" in w for w in result.warnings)


def test_actual_db_templates_and_rag_injection(deps):
    deps.model = model("compare", numbers=["A"])
    deps.ask_fn = Mock(side_effect=fake_ask)
    result = answer("판본 비교", deps=deps)
    assert result.tables and result.tables[0]["title"] == "판본 비교"
    injected = deps.ask_fn.call_args.kwargs["injected_cards"]
    assert injected[0].source_type == "db" and "판본 비교" in injected[0].text
    assert "출시 연도:" in injected[0].text and "부품 수(Rebrickable 기준):" in injected[0].text
    assert "서브테마:" not in injected[0].text and "범위:" not in injected[0].text
    assert "서브테마" in result.tables[0]["columns"] and "이름" in result.tables[0]["columns"]


@pytest.mark.parametrize("route", ["narrative", "compare", "build_archive"])
@pytest.mark.parametrize("version", ["p3-v1", "p3-v2"])
def test_real_graph_with_local_fake_embeddings(deps, route, version):
    from rag_service.vectorstore.store import get_collection, get_docs_collection

    deps.settings["retrieval"]["p3_prompt_version"] = version
    deps.settings["verify"]["p3_max_bullets"] = 3
    embeddings = DeterministicFakeEmbedding(size=8)
    meta = get_collection(deps.settings["collections"]["items"], embeddings, deps.settings)
    docs = get_docs_collection(embeddings, deps.settings)
    meta.add_texts(["세트 A의 DB 정보", "세트 B의 DB 정보"], ids=["A-1", "B-1"],
                   metadatas=[{"name": "A"}, {"name": "B"}])
    p3 = Mock()
    p3.invoke.return_value = AIMessage(content="확인한 사실 [근거 1]")
    deps.model = model(route, numbers=["A"], extra=[{}])
    deps.collection = {"sets": [("A-1", 1)]}
    if route == "build_archive":
        deps.recommend_fn = Mock(return_value=[Recommendation(
            SetInfo("B-1", "B", 2018, 5, "SW", "core"), 1, 1, .8, 5, False)])
    deps.rag_deps = RagDeps(deps.settings, p3, meta, docs)
    events = []
    history = [{"question": "이전 질문", "route": "fact", "answer": "이전 답변"}]
    result = answer("특징을 알려줘", deps=deps, history=history, on_event=lambda n, d: events.append(n))
    assert result.needs_input is None and result.cards
    assert result.cards[0].n == 1
    assert result.cards[0].source_type == ("set_meta" if route == "narrative" else "db")
    if route == "build_archive":
        assert [c.n for c in result.cards] == [1, 2] and result.cards[1].title == "선택 이유"
        assert result.answer.startswith("선택 이유:")
    prompt = "\n".join(m.content for m in p3.invoke.call_args.args[0])
    assert "이전 질문" in prompt and "이전 답변" in prompt
    assert version in prompt
    if version == "p3-v2":
        assert "최대 3개" in prompt and "[선택 세트]" in prompt
        if route == "compare":
            assert "화면에 표로 제공됨: 판본 비교" in prompt
    assert "retrieving" in events and "generating" in events and events.count("done") == 1
    assert "p3" in result.usage["rag"] and "retrieve_ms" in result.timings


def test_actual_filter_service_wiring(deps):
    deps.model = model("filter", extra=[{"parts_max": 7}])
    deps.meta_store = Mock()
    result = answer("7개 이하인 세트", deps=deps)
    assert result.route == "filter" and not result.needs_input
    assert result.set_nums and result.cards and result.tables
    assert all(row[3] <= 7 for row in result.tables[0]["rows"])
    assert not result.verification["items"]


def test_collection_id_and_owned_connections_remain_open(deps):
    deps.collection_store = Mock()
    deps.collection_store.get.return_value = {"sets": [("A-1", 1)], "adjustments": []}
    deps.model = model("build", numbers=["B"])
    result = answer("제작 분석", collection_id="saved", deps=deps)
    deps.collection_store.get.assert_called_once_with("saved")
    assert not result.needs_input and "만 더 있으면 완성할 수 있어요" in result.answer
    assert deps.conn.execute("SELECT 1").fetchone()[0] == 1


def test_router_failure_with_fallback_disabled_stops(deps):
    deps.settings["router"]["llm_fallback"] = False
    deps.model = FakeModel(["bad json"])
    deps.ask_fn = Mock()
    result = answer("정보", selected_set="A", deps=deps)
    assert result.route_source == "fallback" and result.needs_input
    deps.ask_fn.assert_not_called()


def test_compare_deduplicates_same_group_and_keeps_other_groups(deps):
    deps.model = model("compare", numbers=["A", "B"])
    same = TemplateAnswer("unused", [card(9)], [{"title": "표", "formats": {}}], [])
    deps.compare_fn = Mock(return_value=same)
    deps.ask_fn = Mock(side_effect=fake_ask)
    result = answer("판본 비교", deps=deps)
    assert deps.compare_fn.call_count == 2 and len(result.tables) == 1
    assert len(deps.ask_fn.call_args.kwargs["injected_cards"]) == 1
    other = TemplateAnswer("unused", [card(9, "다른 기체")], [{"title": "표2", "formats": {}}], [])
    deps.compare_fn.side_effect = [same, other]
    result = answer("판본 비교", deps=deps)
    assert len(result.tables) == 2
    assert [c.n for c in deps.ask_fn.call_args.kwargs["injected_cards"]] == [1, 2]


def test_eval_24_schema_and_distribution():
    rows = [json.loads(line) for line in (project_root() / "eval/route_questions.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(rows) == len({r["id"] for r in rows}) == 24
    counts = Counter(r["expected_route"] for r in rows)
    assert set(counts) == set(get_args(Route)) and all(3 <= n <= 4 for n in counts.values())
    assert sum(bool(r["selected_set"] or r["history"]) for r in rows) >= 6
    for row in rows:
        assert isinstance(row["question"], str) and row["question"]
        assert isinstance(row["has_collection"], bool) and isinstance(row["history"], list)
        RouterOutputV2(route=row["expected_route"], confidence=1)
        router_messages(row["question"], row["has_collection"], "router-v2", history=row["history"])


def test_cli_help_arguments_and_history_validation(capsys, tmp_path):
    spec = importlib.util.spec_from_file_location("chat_cli", project_root() / "scripts/chat.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--help"])
    assert exit_info.value.code == 0
    help_text = capsys.readouterr().out
    assert all(option in help_text for option in ("--set", "--collection-set", "--history"))
    args = cli.parser().parse_args(["질문", "--set", "A", "--collection-set", "A=2", "--collection-set", "B=1"])
    assert args.collection_set == [("A", 2), ("B", 1)] and args.selected_set == "A"
    for value in ("A=0", "A=-1", "A=bad", "=1", "A"):
        with pytest.raises(SystemExit):
            cli.parser().parse_args(["질문", "--collection-set", value])
    invalid = tmp_path / "history.json"
    invalid.write_text("{}", encoding="utf-8")
    assert cli.main(["질문", "--history", str(invalid)]) == 1


def upstream_error(kind="502"):
    request = httpx.Request("GET", "https://offline.invalid/")
    detail = "SECRET_KEY private_prompt traceback_detail"
    if kind == "connection":
        return APIConnectionError(message=detail, request=request)
    if kind == "timeout":
        return APITimeoutError(request=request)
    if kind == "collector":
        return UpstreamError(detail)
    if kind == "httpx":
        return httpx.ReadTimeout(detail, request=request)
    if kind == "builtin":
        return TimeoutError(detail)
    status = int(kind)
    cls = RateLimitError if status == 429 else InternalServerError if status == 502 else APIStatusError
    return cls(detail, response=httpx.Response(status, request=request), body={"error": detail})


@pytest.mark.parametrize("kind", ["502", "500", "503", "429", "connection", "timeout", "collector", "httpx", "builtin"])
@pytest.mark.parametrize("recovers", [True, False])
def test_transient_route_retry_is_once_and_sanitized(deps, monkeypatch, kind, recovers, capsys):
    deps.model = model("compare", numbers=["A"])
    calls = 0

    def unstable(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1 or not recovers:
            raise upstream_error(kind)
        return fake_ask(*args, **kwargs)

    deps.ask_fn = Mock(side_effect=unstable)
    pause = Mock()
    monkeypatch.setattr(orchestrator, "sleep", pause)
    deps.settings["router"]["retry_backoff_sec"] = .125
    events = []
    result = answer("판본 비교", deps=deps, on_event=lambda n, d: events.append(n))
    assert calls == 2 and deps.model.calls == 1
    pause.assert_called_once_with(.125)
    assert result.route == "compare" and events.count("done") == 1
    if recovers:
        assert result.needs_input is None and len(result.tables) == 1
        assert result.cards and "upstream_unavailable" not in result.warnings
    else:
        assert result.answer == "외부 서비스 응답이 일시적으로 불안정합니다. 잠시 후 다시 시도해 주세요"
        assert result.needs_input == result.answer
        assert result.warnings == ["upstream_unavailable"]
        assert not result.tables and not result.cards
    public = json.dumps(asdict(result), ensure_ascii=False) + repr(capsys.readouterr())
    assert all(secret not in public for secret in ("SECRET_KEY", "private_prompt", "traceback_detail", "Traceback"))


@pytest.mark.parametrize("error", [MissingApiKey("SECRET_KEY private_prompt"),
                                  RuntimeError("SECRET_KEY private_prompt"),
                                  upstream_error("400"), upstream_error("401")])
def test_nontransient_errors_do_not_retry_and_details_are_file_only(deps, monkeypatch, capsys, error):
    deps.model = model("narrative", numbers=["A"])
    deps.ask_fn = Mock(side_effect=error)
    pause = Mock()
    monkeypatch.setattr(orchestrator, "sleep", pause)
    result = answer("특징", deps=deps)
    deps.ask_fn.assert_called_once()
    pause.assert_not_called()
    assert result.needs_input and NO_CITATION_MESSAGE not in result.warnings
    expected = "API 키가 설정되지 않았습니다" if isinstance(error, MissingApiKey) else "처리 중 오류가 발생했습니다"
    assert expected in result.answer
    public = json.dumps(asdict(result), ensure_ascii=False) + repr(capsys.readouterr())
    assert "SECRET_KEY" not in public and "private_prompt" not in public and "Traceback" not in public
    if isinstance(error, RuntimeError) and not isinstance(error, MissingApiKey):
        from pathlib import Path
        detail = Path(deps.settings["router"]["error_log"]).read_text(encoding="utf-8")
        assert "RuntimeError" in detail and "private_prompt" in detail and "Traceback" in detail


@pytest.mark.parametrize("stage", ["collection", "routing", "resolving", "factory"])
def test_unexpected_error_boundary_covers_setup_and_routing(deps, monkeypatch, stage):
    pause = Mock()
    monkeypatch.setattr(orchestrator, "sleep", pause)
    monkeypatch.setattr(orchestrator, "load_settings", lambda: deps.settings)
    failure = RuntimeError("SECRET_KEY private_prompt")
    kwargs = {"deps": deps}
    deps.model = model(numbers=["A"])
    if stage == "collection":
        monkeypatch.setattr(orchestrator, "_collection", Mock(side_effect=failure))
    elif stage == "routing":
        monkeypatch.setattr(orchestrator, "router_messages", Mock(side_effect=failure))
    elif stage == "resolving":
        monkeypatch.setattr(orchestrator, "resolve_sets", Mock(side_effect=failure))
    else:
        monkeypatch.setattr(orchestrator, "default_deps", Mock(side_effect=failure))
        kwargs = {}
    result = answer("정보", **kwargs)
    assert result.answer == "처리 중 오류가 발생했습니다"
    assert not result.cards and not result.tables and not result.warnings
    pause.assert_not_called()


@pytest.mark.parametrize("route,selected,question", [("fact", None, "정보"), ("build", None, "제작"),
                                                     ("fact", "missing", "정보"), ("fact", None, " ")])
def test_needs_input_has_no_citation_warning(deps, route, selected, question):
    deps.model = model(route)
    result = answer(question, selected_set=selected, deps=deps)
    assert result.needs_input and NO_CITATION_MESSAGE not in result.warnings


def test_normal_uncited_answer_keeps_warning(deps):
    deps.model = model("narrative", numbers=["A"])
    deps.ask_fn = Mock(return_value={"answer": "확인되지 않음", "cards": []})
    result = answer("특징", deps=deps)
    assert not result.needs_input and NO_CITATION_MESSAGE in result.warnings


@pytest.mark.parametrize("selected,expected", [("101", "101-1"), ("103", "100-1"),
                                               ("102", "100-1"), (None, "100-1")])
def test_compare_selected_only_when_in_compared_versions(deps, selected, expected):
    for number, name in [("100-1", "Falcon"), ("101-1", "Falcon"),
                          ("102-1", "Falcon Microfighter"), ("103-1", "X-wing")]:
        add_set(deps.conn, number, name)
    deps.conn.execute("CREATE TABLE set_entities(set_num TEXT, wiki_title TEXT, kind TEXT, rule_index INTEGER)")
    deps.conn.executemany("INSERT INTO set_entities VALUES (?, ?, 'vehicle', 0)",
                          [("100-1", "Falcon"), ("101-1", "Falcon"),
                           ("102-1", "Falcon"), ("103-1", "X-wing")])
    deps.model = model("compare", numbers=["100-1"])
    deps.ask_fn = Mock(side_effect=fake_ask)
    result = answer("판본 비교", selected_set=selected, deps=deps)
    assert not result.needs_input
    assert deps.ask_fn.call_args.args[0] == expected and result.set_nums[0] == expected
    assert result.tables[0]["rows"][0][0] == "100-1"
    assert result.tables[0]["rows"][1][0] == "101-1"


def test_compare_selected_in_merged_variant_row(deps):
    add_set(deps.conn, "100-1", "Falcon")
    add_set(deps.conn, "100-2", "Falcon")
    deps.model = model("compare", numbers=["100-1"])
    deps.ask_fn = Mock(side_effect=fake_ask)
    result = answer("판본 비교", selected_set="100-2", deps=deps)
    assert "100-1/100-2" in [row[0] for row in result.tables[0]["rows"]]
    assert deps.ask_fn.call_args.args[0] == "100-2"


def test_cli_unexpected_error_is_sanitized(deps, monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location("chat_feedback_cli", project_root() / "scripts/chat.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)

    @contextmanager
    def configured():
        yield deps

    monkeypatch.setattr(orchestrator, "default_deps", configured)
    monkeypatch.setattr(orchestrator, "answer", Mock(side_effect=RuntimeError("SECRET_KEY private_prompt")))
    monkeypatch.setattr(orchestrator, "load_settings", lambda: deps.settings)
    assert cli.main(["특징"]) == 1
    output = capsys.readouterr()
    assert "처리 중 오류가 발생했습니다" in output.err
    assert "SECRET_KEY" not in output.err + output.out and "Traceback" not in output.err + output.out


@pytest.mark.parametrize("recovers", [True, False])
def test_actual_retrieve_502_boundary(deps, monkeypatch, recovers):
    configure_similarity(deps.settings)
    meta, docs, p3 = Mock(), Mock(), Mock()
    meta.get.return_value = {"ids": ["A-1"], "documents": ["DB 정보"], "metadatas": [{"name": "A"}]}
    docs.similarity_search_with_score.side_effect = [upstream_error(), [] if recovers else upstream_error()]
    p3.invoke.return_value = AIMessage(content="설명 [근거 1]")
    deps.rag_deps = RagDeps(deps.settings, p3, meta, docs)
    deps.model = model("narrative", numbers=["A"])
    pause = Mock()
    monkeypatch.setattr(orchestrator, "sleep", pause)
    result = answer("특징", deps=deps)
    assert docs.similarity_search_with_score.call_count == 2
    pause.assert_called_once_with(deps.settings["router"]["retry_backoff_sec"])
    assert p3.invoke.call_count == int(recovers)
    assert bool(result.needs_input) != recovers
    if not recovers:
        assert result.warnings == ["upstream_unavailable"]


def test_compare_selected_beyond_display_limit(deps, monkeypatch):
    from rag_service.rag import templates

    for number in ("100-1", "101-1"):
        add_set(deps.conn, number, "Unique vehicle")
    deps.settings["compare"]["table_max_rows"] = 1
    monkeypatch.setattr(templates, "load_settings", lambda: deps.settings)
    deps.model = model("compare", numbers=["100"])
    deps.ask_fn = Mock(side_effect=fake_ask)
    result = answer("판본 비교", selected_set="101", deps=deps)
    assert result.tables[0]["rows"][0][0] == "100-1" and len(result.tables[0]["rows"]) == 1
    assert deps.ask_fn.call_args.args[0] == "101-1"


def test_log_write_failure_never_prints_traceback(deps, tmp_path, capsys):
    blocked = tmp_path / "blocked"
    blocked.write_text("file prevents directory creation", encoding="utf-8")
    deps.settings["router"]["error_log"] = str(blocked / "error.log")
    deps.model = model("narrative", numbers=["A"])
    deps.ask_fn = Mock(side_effect=RuntimeError("SECRET_KEY private_prompt"))
    result = answer("특징", deps=deps)
    assert result.answer == "처리 중 오류가 발생했습니다"
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("route", ["narrative", "compare", "build_archive"])
def test_numeric_verification_generated_routes_and_log(deps, route):
    deps.model = model(route, numbers=["A"], extra=[{}])
    deps.collection = {"sets": [("A-1", 1)]}
    deps.recommend_fn = Mock(return_value=[Recommendation(
        SetInfo("B-1", "B", 2018, 5, "SW", "core"), 1, 1, .8, 5, False)])
    deps.ask_fn = Mock(return_value={"answer": "2018년입니다 [근거 7].\n2017년입니다 [근거 8].",
                                    "cards": [card(7, "2017"), card(8, "2017")]})
    result = answer("특징", deps=deps)
    assert result.needs_input is None
    assert result.answer == "2017년입니다 [근거 8]." and [c.n for c in result.cards] == [8]
    assert result.verification == {"checked": 2, "removed": 1,
                                   "items": [{"value": "2018", "sentence_index": 0}]}
    from pathlib import Path

    record = json.loads(Path(deps.settings["verify"]["log_path"]).read_text(encoding="utf-8"))
    assert record["violations"] == [{"sentence": "2018년입니다 [근거 7].", "values": ["2018"]}]


def test_numeric_verification_template_preserves_answer(deps):
    deps.model = model("fact", numbers=["A"])
    deps.fact_fn = Mock(return_value=TemplateAnswer("2018년입니다 [근거 7].", [card(7, "2017")], [], []))
    result = answer("출시 연도", deps=deps)
    assert result.answer == "2018년입니다 [근거 7]." and result.verification["removed"] == 0
    assert result.verification["items"] == [{"value": "2018", "sentence_index": 0}]
    assert any("템플릿 수치 검증에 실패" in warning for warning in result.warnings)


@pytest.mark.parametrize("route,question", [("fact", "부품 몇 개"), ("build", "제작"), ("set_ops", "부품 비교")])
def test_actual_templates_pass_numeric_verification(deps, route, question):
    deps.model = model(route, numbers=["A", "B"] if route == "set_ops" else ["A"])
    deps.collection = {"sets": [("B-1", 1)]}
    result = answer(question, deps=deps)
    assert result.needs_input is None and result.cards
    assert not result.verification["items"]


def test_numeric_removal_no_citation_and_disable(deps):
    deps.model = model("narrative", numbers=["A"])
    deps.ask_fn = Mock(return_value={"answer": "2018년 [근거 7]", "cards": [card(7, "2017")]})
    result = answer("설명", deps=deps)
    assert result.answer == "제공된 자료에서 확인되지 않습니다" and not result.cards
    assert NO_CITATION_MESSAGE in result.warnings
    deps.settings["verify"]["enabled"] = False
    result = answer("설명", deps=deps)
    assert result.answer == "2018년 [근거 7]" and not result.verification["items"]


def test_v2_context_reaches_p3_without_changing_retrieval_question(deps):
    from rag_service.rag.prompts import p3_messages

    deps.settings["retrieval"]["p3_prompt_version"] = "p3-v2"
    deps.settings["verify"]["p3_max_bullets"] = 2
    deps.model = model("compare", numbers=["A"])

    def inspect(number, question, **kwargs):
        assert question == "판본 비교"
        messages = p3_messages(question, kwargs["injected_cards"], history_turns=0, version="p3-v2")
        assert "최대 2개" in messages[0].content
        assert "화면에 표로 제공됨: 판본 비교" in messages[-1].content
        assert '"set_num": "A-1"' in messages[-1].content
        return fake_ask(number, question, **kwargs)

    deps.ask_fn = inspect
    assert not answer("판본 비교", deps=deps).needs_input


@pytest.mark.parametrize("route", ["narrative", "filter", "build_archive"])
def test_verification_includes_selected_resolved_and_result_sets(deps, route):
    for number in ("75301-1", "75257-1", "75192-1"):
        add_set(deps.conn, number)
    deps.model = model(route, numbers=["75257-1"], extra=[{}])
    deps.collection = {"sets": [("A-1", 1)]}
    deps.recommend_fn = Mock(return_value=[Recommendation(
        SetInfo("75192-1", "Falcon", 2018, 5, "SW", "core"), 1, 1, .8, 5, False)])
    numbers = "75301과 75257" + ("과 75192" if route != "narrative" else "")
    text = f"{numbers}의 특징입니다 [근거 7]."
    deps.ask_fn = Mock(return_value={"answer": text, "cards": [card(7, "특징 설명")]})
    deps.search_fn = Mock(return_value=SimpleNamespace(
        template=TemplateAnswer(text, [card(7, "특징 설명")], [], []),
        set_nums=["75192-1"], warnings=[], usage={}, elapsed_ms=0))
    result = answer("특징을 알려줘", selected_set="75301-1", deps=deps)
    assert result.needs_input is None and result.answer == text and result.cards
    assert result.verification == {"checked": 2 if route == "narrative" else 3, "removed": 0, "items": []}
    assert result.set_nums == (["75257-1"] if route == "narrative" else ["75192-1"])
