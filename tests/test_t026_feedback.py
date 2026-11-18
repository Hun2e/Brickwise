"""비공개 평가셋 없이 연도·영어 탐색·후속 추천 회귀를 검증한다."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from rag_service.rag.schemas import RouterOutputV2
from rag_service.services.context_resolver import resolve_context
from rag_service.services.dialogue_state import DialogueState, ShownList, Subject
from rag_service.services.orchestrator import _rules, answer
from rag_service.services.resolve import explicit_set_tokens, resolve_sets
from test_db_queries import add_set, conn
from test_orchestrator_offline import deps, model, template


@pytest.fixture
def numbered(deps):
    for number, scope in (("2021-1", None), ("75301-1", "core"),
                          ("1234-1", "compare_only"), ("90000-1", None)):
        add_set(deps.conn, number, name=number, scope=scope)
    return deps


def current(kind="search"):
    return DialogueState(subject=Subject("A-1", "ui", None),
                         last_list=ShownList(kind, None, ("B-1", "C-1")))


@pytest.mark.parametrize("question", [
    "Which of them came out in 2021?", "Among them, released since 2021?",
    "Of those, released before 2021?", "그중 2021년", "그중 2021",
])
@pytest.mark.parametrize("hint", ["2021", "2021-1"])
def test_year_refinement_cannot_change_subject(numbered, question, hint):
    context = resolve_context(question, current(), conn=numbered.conn, settings=numbered.settings)
    assert context.subject is None and not context.clarify
    assert context.refine["allow"] == ["B-1", "C-1"]
    assert "subject" not in context.state_delta
    out = RouterOutputV2(route="fact", set_nums=[hint], confidence=.1)
    resolved = resolve_sets("filter", out, question, "A-1", numbered.conn)
    assert resolved.set_nums == [] and not resolved.needs_input
    assert _rules(out, question, "A-1", False, numbered, "llm", context)[:2] == (
        "filter", "rule:list_refine")


@pytest.mark.parametrize("question", [
    "2021년에 나온 세트 찾아줘", "Find sets released in 2021",
    "Search sets after 2035", "Show me sets from 1950", "List sets before 1949",
    "Find sets since 2040", "Find sets from year 2040", "Find sets in 75301",
    "1000개 세트 찾아줘",
])
def test_year_and_quantity_search(numbered, question):
    context = resolve_context(question, current(), conn=numbered.conn, settings=numbered.settings)
    assert context.subject is None and not context.clarify
    route, source, resolved = _rules(RouterOutputV2(route="narrative", confidence=.1),
        question, None, False, numbered, "llm", context)
    assert (route, source) == ("filter", "rule:search_cue")
    assert not resolved.set_nums and not resolved.needs_input


@pytest.mark.parametrize("question,number", [
    ("75301 부품 몇 개야", "75301-1"), ("75301-1 부품 몇 개야", "75301-1"),
    ("1234 부품 수", "1234-1"),
])
def test_explicit_scoped_number_is_preserved(numbered, question, number):
    context = resolve_context(question, current(), conn=numbered.conn, settings=numbered.settings)
    assert context.subject == number and context.subject_source == "explicit"
    assert not context.clarify
    result = resolve_sets("fact", RouterOutputV2(route="fact", confidence=.95), question, "A-1", numbered.conn)
    assert result.set_nums == [number] and not result.needs_input


@pytest.mark.parametrize("question,message", [
    ("2021-1 설명해줘", "범위 밖"), ("90000 설명해줘", "범위 밖"),
    ("90000-1 설명해줘", "범위 밖"), ("99999 설명해줘", "데이터에 없는"),
])
def test_outside_or_missing_number_prompts_without_target_change(numbered, question, message):
    context = resolve_context(question, current(), conn=numbered.conn, settings=numbered.settings)
    assert context.subject is None and "subject" not in context.state_delta
    assert message in context.clarify["question"]
    result = resolve_sets("narrative", RouterOutputV2(route="narrative", confidence=.95), question, "A-1", numbered.conn)
    assert not result.set_nums and message in result.needs_input


@pytest.mark.parametrize("question", ["1950", "2035", "2021", "1234567", "10000개", "in 1949", "2040년"])
def test_non_identifier_tokens(question):
    assert explicit_set_tokens(question) == []


@pytest.mark.parametrize("question", [
    "Find sets including Luke", "SEARCH sets with Luke", "Show me sets that include Luke",
    "List sets including Luke", "Which sets include Luke?", "Sets with Luke", "Sets that include Luke",
])
def test_english_search_dispatch_ignores_stale_subject(deps, question):
    context = resolve_context(question, current(), conn=deps.conn, settings=deps.settings)
    assert context.subject is None
    deps.model = model("narrative", confidence=.1)
    deps.search_fn = Mock(return_value=SimpleNamespace(
        template=template(), set_nums=["B-1"], warnings=[], usage={}, elapsed_ms=0))
    deps.ask_fn = Mock()
    response = answer(question, deps=deps, context=context)
    assert (response.route, response.route_source) == ("filter", "rule:search_cue")
    assert response.set_nums == ["B-1"] and not response.needs_input
    deps.search_fn.assert_called_once()
    deps.ask_fn.assert_not_called()


@pytest.mark.parametrize("question", [
    "Who is included with Luke?", "including Luke", "with Luke", "include Luke",
    "Tell me about Pathfinder", "The specialist with Luke",
])
def test_character_words_alone_do_not_trigger_search(deps, question):
    context = resolve_context(question, current(), conn=deps.conn, settings=deps.settings)
    assert context.subject == "A-1"
    assert _rules(RouterOutputV2(route="narrative", confidence=.95), question,
                  "A-1", False, deps, "llm", context)[0] == "narrative"


@pytest.mark.parametrize("question", ["다시 추천", "또 추천", "다른 거 추천", "Recommend again"])
@pytest.mark.parametrize("kind", ["search", "recommend"])
def test_recommend_followup_builds_list_not_selected_analysis(deps, question, kind):
    deps.collection = {"sets": [("A-1", 1)]}
    deps.model = model("narrative", numbers=["A-1"], confidence=.1)
    deps.analysis_fn = Mock(side_effect=AssertionError("재추천은 대상 분석이 아님"))
    context = resolve_context(question, current(kind), conn=deps.conn, settings=deps.settings)
    response = answer(question, deps=deps, context=context)
    assert (response.route, response.route_source) == ("build", "rule:recommend_followup")
    assert not response.needs_input and response.items
    assert response.state_delta["last_list"]["kind"] == "recommend"
    assert "subject" not in response.state_delta
    deps.analysis_fn.assert_not_called()


def test_recommend_followup_after_collection_emptied(deps):
    context = resolve_context("recommend again", current("recommend"), conn=deps.conn, settings=deps.settings)
    route, source, resolved = _rules(RouterOutputV2(route="narrative", confidence=.95), "recommend again",
                                   None, False, deps, "llm", context)
    assert (route, source) == ("build", "rule:no_collection")
    assert "먼저 등록" in resolved.needs_input


def test_recommend_followup_without_collection_or_recommendation_is_not_forced(deps):
    assert _rules(RouterOutputV2(route="narrative", confidence=.95), "recommend again",
                  None, False, deps, "llm")[1] != "rule:recommend_followup"


@pytest.mark.parametrize("question", ["X-wing 설명해줘", "X-wing은 뭐야?", "엑스윙 설명해줘", "Named Fighter 설명해줘"])
def test_entity_and_name_candidates_are_scoped(deps, question):
    deps.conn.execute("CREATE TABLE set_entities(set_num TEXT, wiki_title TEXT, kind TEXT)")
    deps.conn.execute("CREATE TABLE ko_aliases(alias TEXT, canonical_en TEXT, kind TEXT)")
    deps.conn.execute("INSERT INTO ko_aliases VALUES ('엑스윙', 'X-wing', 'vehicle')")
    for number, scope in (("70001-1", "core"), ("70002-1", "compare_only"),
                          ("6520657-1", None), ("SWCOMIC1-1", None), ("TRUXWING-1", None)):
        add_set(deps.conn, number, name="Named Fighter", scope=scope)
        deps.conn.execute("INSERT INTO set_entities VALUES (?, 'X-wing', 'vehicle')", (number,))
    context = resolve_context(question, current(), conn=deps.conn, settings=deps.settings)
    assert context.clarify["options"] == ["70001-1", "70002-1"]
    assert context.subject is None
    deps.conn.execute("DELETE FROM set_scope WHERE set_num='70001-1'")
    context = resolve_context(question, current(), conn=deps.conn, settings=deps.settings)
    assert context.subject == "70002-1" and not context.clarify
    result = resolve_sets("compare", RouterOutputV2(route="compare", entities=["X-wing"], confidence=.95),
                          "X-wing 판본 비교", None, deps.conn)
    assert result.set_nums == ["70002-1"]


def test_english_refinement_dispatches_with_previous_allow_list(numbered):
    question = "Which of them came out in 2021?"
    context = resolve_context(question, current(), conn=numbered.conn, settings=numbered.settings)
    numbered.model = model("fact", numbers=["2021-1"], extra=[{"year_from": 2021, "year_to": 2021}])
    numbered.meta_store = object()
    # 실제 검색 서비스를 대역 필터 추출과 함께 실행해 이전 목록 제한까지 검증한다.
    numbered.conn.execute("UPDATE sets SET year=2021 WHERE set_num='B-1'")
    response = answer(question, context=context, deps=numbered)
    assert response.route == "filter" and not response.needs_input
    assert response.set_nums == ["B-1"]
    assert response.state_delta["subject"]["set_num"] == "B-1"
