"""문맥 충돌·다턴 상태와 라우터 연결을 외부 호출 없이 검증한다."""

import json
from dataclasses import replace
from unittest.mock import Mock

import pytest

from rag_service.config import load_settings
from rag_service.llm import MissingApiKey
from rag_service.rag.prompts import router_messages
from rag_service.rag.templates import answer_build_analysis, answer_build_recommend
from rag_service.services.context_resolver import ResolvedContext, resolve_context
from rag_service.services.dialogue_state import DialogueState, Pending, ShownList, Subject
from rag_service.services.orchestrator import answer
from test_db_queries import add_set, conn, real_conn
from test_orchestrator_offline import deps, model, fake_ask
from test_structured_offline import FakeModel


def state(subject="A-1", items=("B-1", "C-1"), pending=False):
    return DialogueState(Subject(subject, "ui", None) if subject else None,
        ShownList("search", None, items), pending=Pending("target", ("C-1", "B-1"),
        {"intent": "fact", "question": "부품 몇 개야?"}) if pending else None)


def resolve(question, current, deps):
    return resolve_context(question, current, conn=deps.conn, settings=deps.settings)


def updated(current, delta):
    payload = json.loads(current.to_json())
    payload.update(delta)
    value = DialogueState.from_json(json.dumps(payload))
    assert json.loads(value.to_json()) == payload
    return value


@pytest.mark.parametrize("question,current,subject,source,clarify,resume", [
    ("A-1 첫 번째 그거 부품", state(), "A-1", "explicit", False, False),
    ("첫 번째 그거 부품", state(), "B-1", "list", False, False),
    ("그거 부품", state(), "A-1", "state", False, False),
    ("마지막", state(), "C-1", "list", False, False),
    ("2번", state(), "C-1", "list", False, False),
    ("1위", state(), "B-1", "list", False, False),
    ("첫 번째", state(pending=True), "C-1", "list", False, True),
    ("2", state(pending=True), "B-1", "list", False, True),
    ("B-1 첫 번째", state(pending=True), "B-1", "explicit", False, True),
    ("A-1 첫 번째", state(pending=True), "A-1", "explicit", False, False),
    ("그거", state(subject=None, items=("B-1",)), "B-1", "list", False, False),
    ("그거", state(subject=None), None, None, True, False),
    ("첫 번째", state(items=()), None, None, True, False),
    ("세 번째", state(), None, None, True, False),
    ("0번", state(), None, None, True, False),
    ("부품 몇 개?", state(pending=True), "A-1", "state", False, False),
    ("내 부품으로 다른 건 뭐 만들 수 있어?", state(), None, None, False, False),
    ("루크가 들어 있는 엑스윙 찾아줘", state(), None, None, False, False),
])
def test_priority_table(deps, question, current, subject, source, clarify, resume):
    result = resolve(question, current, deps)
    assert (result.subject, result.subject_source, bool(result.clarify), bool(result.resume)) == (
        subject, source, clarify, resume)
    assert result.collection_target == subject
    if current.pending:
        assert result.state_delta["pending"] is None


def test_explicit_outside_list_comparison_and_invalid_numbers(deps):
    result = resolve("그중 A-1 알려줘", state(), deps)
    assert result.subject == "A-1" and result.notes == ("목록 밖 세트입니다",) and not result.refine
    assert resolve("B-1 A-1 비교", state(), deps).compare == ("B-1", "A-1")
    assert resolve("이 세트랑 B-1 비교", state(), deps).compare == ("B-1", "A-1")
    assert resolve("B-1 C-1 설명해줘", state(), deps).clarify
    assert resolve("99999 부품 수", state(), deps).clarify


def test_aliases_do_not_choose_representative_arbitrarily(deps):
    deps.conn.execute("CREATE TABLE ko_aliases(alias TEXT, canonical_en TEXT, kind TEXT)")
    deps.conn.execute("CREATE TABLE set_entities(set_num TEXT, wiki_title TEXT)")
    deps.conn.execute("INSERT INTO ko_aliases VALUES ('시험별칭', 'Ship', 'vehicle')")
    deps.conn.execute("INSERT INTO set_entities VALUES ('B-1', 'Ship')")
    assert resolve("시험별칭 부품", state(), deps).subject == "B-1"
    deps.conn.execute("INSERT INTO set_entities VALUES ('C-1', 'Ship')")
    assert resolve("시험별칭 부품", state(), deps).clarify["options"] == ["B-1", "C-1"]


@pytest.mark.parametrize("candidates", [("B-1",), ("B-1", "C-1")])
def test_character_in_current_set_does_not_select_another_set(deps, candidates):
    deps.conn.execute("CREATE TABLE ko_aliases(alias TEXT, canonical_en TEXT, kind TEXT)")
    deps.conn.execute("CREATE TABLE set_entities(set_num TEXT, wiki_title TEXT)")
    deps.conn.execute("INSERT INTO ko_aliases VALUES ('다스 베이더', 'Darth Vader', 'character')")
    deps.conn.executemany("INSERT INTO set_entities VALUES (?, 'Darth Vader')", [(n,) for n in candidates])
    result = resolve("이 세트에 다스 베이더 미니피그 있어?", state(), deps)
    assert result.subject == "A-1" and result.subject_source == "state" and not result.clarify
    assert resolve("B-1 이 세트에 다스 베이더 미니피그 있어?", state(), deps).subject == "B-1"


@pytest.mark.parametrize("question", ["색상은 무시하면 얼마나 부족해?", "색깔을 무시하면?", "색상은 상관 없으면?", "색 무시"])
def test_mode_followups(deps, question):
    result = resolve(question, state(), deps)
    assert result.subject == "A-1" and result.mode == "ignore_color" and result.mode_followup
    assert result.state_delta == {"mode": "ignore_color"}
    rec = {"mode": "exact", "top_n": 3, "scope": "core"}
    current = DialogueState(last_list=ShownList("recommend", None, ("B-1", "C-1"), rec=rec))
    assert resolve(question, current, deps).recommend == {**rec, "mode": "ignore_color"}
    assert resolve(question, DialogueState(), deps).clarify


def test_pending_confirmation_quantity_and_abandonment(deps):
    request = {"intent": "own", "op": "set", "qty": 1, "question": "그거 하나 있어", "mode": "ignore_color"}
    current = DialogueState(pending=Pending("confirm", ("B-1",), request))
    yes = resolve("응", current, deps)
    assert yes.resume == {**request, "confirmed": True} and yes.mode == "ignore_color"
    assert resolve("아니", current, deps).resume["confirmed"] is False
    current = replace(current, pending=Pending("quantity", ("B-1",), request))
    assert resolve("두 개", current, deps).resume["qty"] == 2
    result = resolve("A-1의 부품 몇 개?", current, deps)
    assert result.subject == "A-1" and result.resume is None and result.state_delta["pending"] is None


def test_clarify_returns_candidate_cards_without_llm(deps):
    deps.model = Mock(side_effect=AssertionError("must not call LLM"))
    context = resolve("그거 부품 몇 개야?", state(subject=None), deps)
    response = answer("그거 부품 몇 개야?", context=context, deps=deps)
    assert response.needs_input and response.set_nums == ["B-1", "C-1"]
    assert [item["rank"] for item in response.items] == [1, 2]
    next_state = updated(state(subject=None), response.state_delta)
    assert next_state.pending.options == ("B-1", "C-1")
    context = resolve("첫 번째", next_state, deps)
    deps.model = model("fact")
    response = answer("첫 번째", context=context, deps=deps)
    assert response.route == "fact" and response.set_nums == ["B-1"] and not response.needs_input
    assert response.state_delta["pending"] is None


def test_router_v4_preserves_v3_and_separates_context_cache(deps):
    v3 = router_messages("q", False, "router-v3")
    v4 = router_messages("q", False, "router-v4", context={"mode": "exact"})
    assert "context" not in json.loads(v3[1].content)
    assert "경계 규칙" in v4[0].content and json.loads(v4[1].content)["context"] == {"mode": "exact"}
    payload = {"route": "fact", "confidence": .95, "set_nums": [], "set_refs": [], "entities": []}
    deps.model = FakeModel([json.dumps(payload), json.dumps(payload)])
    for mode_value in ("exact", "ignore_color", "exact"):
        context = resolve("부품 몇 개?", replace(state(), mode=mode_value), deps)
        response = answer("부품 몇 개?", context=context, deps=deps)
        assert response.route == "fact" and not response.needs_input
    assert deps.model.calls == 2 and response.usage["router"]["cached"]


def test_context_target_and_mode_override_ui_and_model(deps):
    deps.collection = {"sets": [("A-1", 1)]}
    deps.model = model("narrative", numbers=["C-1"])
    context = resolve("색상은 무시하면 얼마나 부족해?", state(subject="B-1"), deps)
    response = answer("색상은 무시하면 얼마나 부족해?", selected_set="C-1", context=context, deps=deps)
    assert response.route == "build" and response.set_nums == ["B-1"] and not response.needs_input
    assert response.state_delta["mode"] == "ignore_color" and "정확 일치" in response.answer


def test_missing_key_discards_uncommitted_delta(deps, monkeypatch):
    def no_key(*args):
        raise MissingApiKey()
    monkeypatch.setattr("rag_service.services.orchestrator.get_chat_model", no_key)
    response = answer("B-1 부품 몇 개?", context=resolve("B-1 부품 몇 개?", state(), deps), deps=deps)
    assert "API 키" in response.needs_input and response.state_delta == {} and not response.items


@pytest.mark.parametrize("mode_value", ["exact", "ignore_color"])
def test_build_completion_wording(deps, mode_value):
    for collection, target in (({"sets": [("A-1", 1)]}, "B-1"), ({"sets": [("A-1", 1)]}, "A-1")):
        response = answer_build_analysis(deps.conn, collection, target, mode_value)
        missing_zero = "부족 수량: 0개" in response.cards[0].text
        assert ("만들 수 있어요" in response.answer) == missing_zero
        if not missing_zero:
            assert "만 더 있으면 완성할 수 있어요" in response.answer
    rec = answer_build_recommend(deps.conn, {"sets": [("A-1", 1)]}, mode_value, {})
    assert ("만들 수 있어요" in rec.answer) == (rec.items[0]["missing"] == 0)
    assert [r[0] for r in rec.tables[0]["rows"]] == [item["set_num"] for item in rec.items]


def test_representative_real_db_scenario(deps, real_conn):
    deps.conn = real_conn  # 읽기 전용 실데이터, 캐시·로그는 deps의 tmp_path에 둔다.
    deps.meta_store = object()
    deps.ask_fn = Mock(side_effect=fake_ask)
    current = DialogueState()

    def turn(question, route, extra=()):
        nonlocal current
        context = resolve(question, current, deps)
        deps.model = model(route, extra=extra)
        response = answer(question, context=context, deps=deps)
        assert not response.needs_input, response
        current = updated(current, response.state_delta)
        return context, response

    context, response = turn("루크가 들어 있는 엑스윙 찾아줘", "filter",
                             [{"minifig": "Luke", "name_keywords": ["X-wing"]}])
    assert context.subject is None and current.last_list.kind == "search"
    assert current.last_list.items == tuple(item["set_num"] for item in response.items)
    assert current.last_list.spec["minifig"] == "Luke"
    previous = current.last_list.items
    context, response = turn("그중 2021년에 나온 건?", "filter", [{"year_from": 2021, "year_to": 2021}])
    assert context.refine["allow"] == list(previous)
    assert response.set_nums == ["75301-1"] and current.subject.set_num == "75301-1"
    before = current
    context, response = turn("부품이 몇 개야?", "fact")
    assert context.subject == "75301-1" and response.state_delta == {} and current == before
    deps.collection = {"sets": [("75301-1", 1)]}
    context, response = turn("내 부품으로 다른 건 뭐 만들 수 있어?", "build")
    assert context.subject is None and "75301-1" not in response.set_nums
    assert current.subject == before.subject and current.last_list.kind == "recommend"
    first = response.items[0]["set_num"]
    context, response = turn("첫 번째 건 어떤 기체야?", "narrative")
    assert context.subject == first and current.subject.set_num == first and current.subject.source == "list"
    assert deps.ask_fn.call_args.args[0] == first
    context, response = turn("색상은 무시하면 얼마나 부족해?", "narrative")
    assert context.mode == "ignore_color" and response.route == "build"
    assert current.mode == "ignore_color" and current.subject.set_num == first
    assert "정확 일치 기준" in response.answer


def test_refine_zero_multiple_then_select(deps):
    deps.meta_store = object()
    current = state(items=("B-1", "A-1", "C-1"))
    context = resolve("그중 3000년에 나온 건?", current, deps)
    deps.model = model("filter", extra=[{"year_from": 3000, "year_to": 3000}])
    response = answer("그중 3000년에 나온 건?", context=context, deps=deps)
    assert "목록의 출시 연도" in response.answer and response.state_delta == {}
    assert response.cards and response.cards[0].source_type == "db"
    context = resolve("그중 500개 이하만", current, deps)
    deps.model = model("filter", extra=[{"parts_max": 500}])
    response = answer("그중 500개 이하만", context=context, deps=deps)
    assert response.needs_input and response.set_nums == list(current.last_list.items)
    current = updated(current, response.state_delta)
    assert current.last_list.kind == "search" and current.pending.kind == "target"
    assert current.last_list.spec["parts_max"] == 500
    deps.model = model("narrative")
    context = resolve("첫 번째", current, deps)
    response = answer("첫 번째", context=context, deps=deps)
    assert not response.needs_input and response.route == "fact" and response.set_nums == ["B-1"]


def test_mode_without_subject_reuses_recommendation_parameters_and_fresh_collection(deps):
    current = DialogueState(last_list=ShownList("recommend", None, ("B-1",),
        rec={"mode": "exact", "top_n": 1, "scope": "core,compare_only"}))
    context = resolve("색상은 무시하면?", current, deps)
    deps.model = model("build")
    store = Mock()
    store.get.return_value = {"sets": [("A-1", 1)]}
    deps.collection_store = store
    response = answer("색상은 무시하면?", context=context, deps=deps, collection_id="c")
    assert not response.needs_input and len(response.items) == 1
    assert response.state_delta["last_list"]["rec"] == {"mode": "ignore_color", "top_n": 1, "scope": "core,compare_only"}
    store.get.return_value = {"sets": [("B-1", 1)]}
    again = answer("색상은 무시하면?", context=context, deps=deps, collection_id="c")
    assert "B-1" not in again.set_nums and store.get.call_count == 2


def test_missing_collection_does_not_create_target_pending(deps):
    question = "B-1 색상은 무시하면 얼마나 부족해?"
    context = resolve(question, state(), deps)
    deps.model = model("build")
    response = answer(question, context=context, deps=deps)
    assert "먼저 등록" in response.needs_input
    assert response.state_delta["subject"]["set_num"] == "B-1"
    assert response.state_delta.get("pending") is None and "mode" not in response.state_delta


def test_recommendation_refine_stays_in_shown_list(deps):
    deps.meta_store = object()
    deps.collection = {"sets": [("A-1", 1)]}
    for number, year, count in (("11001-1", 2020, 2), ("11002-1", 2021, 4), ("11003-1", 2021, 9)):
        add_set(deps.conn, number, number, year, count, parts=[("p999", 1, count)])
    current = DialogueState(last_list=ShownList("recommend", None, ("11002-1", "11001-1", "11003-1"),
        rec={"mode": "exact", "top_n": 3, "scope": "core"}))
    context = resolve("그중 부족 4개 이하", current, deps)
    deps.model = model("filter", extra=[{}])
    response = answer("그중 부족 4개 이하", context=context, deps=deps)
    assert response.set_nums == ["11002-1", "11001-1"]
    assert [item["missing"] for item in response.items] == [4, 2]
    current = updated(current, response.state_delta)
    assert current.last_list.kind == "recommend" and current.pending.kind == "target"
    context = resolve("그중 2021년에 나온 건?", current, deps)
    deps.model = model("filter", extra=[{"year_from": 2021, "year_to": 2021}])
    response = answer("그중 2021년에 나온 건?", context=context, deps=deps)
    assert response.set_nums == ["11002-1"] and not response.needs_input
    assert response.state_delta["subject"]["set_num"] == "11002-1"


@pytest.mark.parametrize("mode_value", [None, "ignore_color"])
def test_refined_recommendation_records_effective_mode(deps, mode_value):
    deps.meta_store = object()
    deps.collection = {"sets": [("A-1", 1)]}
    context = ResolvedContext(None, None, mode=mode_value, refine={"base_spec": None,
        "allow": ["B-1"], "kind": "recommend", "rec": {"mode": "exact", "top_n": 1, "scope": "core"}})
    deps.model = model("filter", extra=[{"parts_max": 500}])
    response = answer("그중 500개 이하", context=context, deps=deps)
    assert not response.needs_input and response.items
    effective = mode_value or deps.settings["planner"]["default_mode"]
    assert response.state_delta["last_list"]["rec"]["mode"] == effective
    assert response.state_delta["mode"] == effective


@pytest.mark.parametrize("question,route,expected", [
    ("A-1의 캐릭터는 어떤 역할이야?", "fact", "narrative"),
    ("A-1 크기 알려줘", "narrative", "fact"),
    ("A-1 B-1 부품 수 비교", "fact", "compare"),
    ("A-1 B-1 공통 부품", "narrative", "set_ops"),
])
def test_v4_keeps_t019_rules_with_context(deps, question, route, expected):
    deps.model = model(route)
    deps.ask_fn = Mock(side_effect=fake_ask)
    context = resolve(question, state(), deps)
    response = answer(question, context=context, deps=deps)
    assert response.route == expected and not response.needs_input
