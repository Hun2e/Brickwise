"""T-019의 경계 규칙과 결정적 응답을 네트워크 없이 검증한다."""

from unittest.mock import Mock

import pytest

from rag_service.config import load_settings
from rag_service.data_prep.unsupported_attrs import unsupported_answer
from rag_service.eval import common
from rag_service.planner.recommend import Recommendation
from rag_service.planner.sets import SetInfo
from rag_service.rag.prompts import router_messages
from rag_service.rag.retrieve import _merge
from rag_service.rag.templates import answer_fact
from rag_service.services.orchestrator import answer
from test_db_queries import add_set, conn, real_conn
from test_orchestrator_offline import deps, fake_ask, model
from test_retrieve_offline import hit


@pytest.mark.parametrize("version", ["router-v2", "router-v3"])
@pytest.mark.parametrize("initial", ["fact", "narrative"])
def test_character_question_with_set_number_uses_narrative(deps, version, initial):
    deps.settings["router"]["prompt_version"] = version
    add_set(deps.conn, "75292-1", "The Razor Crest")
    deps.model = model(initial, numbers=["75292"])
    deps.ask_fn = Mock(side_effect=fake_ask)
    deps.fact_fn = Mock()
    result = answer("75292 레이저 크레스트에 나오는 딘 자린은 어떤 인물이야?", deps=deps)
    assert (result.route, result.route_source) == (
        "narrative", "rule:fact_without_attribute" if initial == "fact" else "llm")
    assert not result.needs_input and result.set_nums == ["75292-1"]
    deps.ask_fn.assert_called_once()
    deps.fact_fn.assert_not_called()


@pytest.mark.parametrize("version", ["router-v2", "router-v3"])
@pytest.mark.parametrize("question", [
    "Suggest something I can build from my parts and explain the ship's backstory",
    "만들 수 있는 거 추천해주고, 그거 영화에서 뭐 했는지도",
    "보유 부품으로 만들 수 있는 기체를 추천하고 역사도 알려줘",
])
def test_build_archive_preserves_english_and_colloquial_intent(deps, version, question):
    deps.settings["router"]["prompt_version"] = version
    deps.model = model("build_archive", extra=[{}])
    deps.collection = {"sets": [("A-1", 1)]}
    rec = Recommendation(SetInfo("B-1", "First", 2018, 5, "Star Wars", "core"), 1, 1, .8, 5, False)
    deps.recommend_fn = Mock(return_value=[rec])
    deps.ask_fn = Mock(side_effect=fake_ask)
    deps.build_fn = Mock()
    result = answer(question, deps=deps)
    assert result.route == "build_archive" and not result.needs_input
    assert result.set_nums == ["B-1"]
    assert deps.ask_fn.call_args.args[0] == "B-1"
    deps.build_fn.assert_not_called()


@pytest.mark.parametrize("version", ["router-v2", "router-v3"])
def test_build_archive_low_confidence_still_falls_back(deps, version):
    deps.settings["router"]["prompt_version"] = version
    deps.model = model("build_archive", confidence=deps.settings["router"]["min_confidence"] - .01)
    deps.collection = {"sets": [("A-1", 1)]}
    deps.ask_fn = Mock(side_effect=fake_ask)
    deps.recommend_fn = Mock()
    result = answer("만들 수 있는 거 추천해주고, 그거 영화에서 뭐 했는지도", selected_set="A", deps=deps)
    assert (result.route, result.route_source) == ("narrative", "rule:low_confidence")
    deps.recommend_fn.assert_not_called()


@pytest.mark.parametrize("version", ["router-v2", "router-v3"])
@pytest.mark.parametrize("question", ["부품 몇 개", "언제 출시", "피겨 몇 개", "How many PIECES?"])
def test_fact_attribute_controls_remain_fact(deps, version, question):
    deps.settings["router"]["prompt_version"] = version
    deps.model = model("fact", numbers=["A"])
    deps.ask_fn = Mock()
    result = answer(question, deps=deps)
    assert (result.route, result.route_source) == ("fact", "llm")
    assert not result.needs_input and result.cards
    deps.ask_fn.assert_not_called()


@pytest.mark.parametrize("version", ["router-v2", "router-v3"])
@pytest.mark.parametrize("initial", ["fact", "compare"])
@pytest.mark.parametrize("question", ["75313하고 75288 구성 차이", "75313하고 75288 부품 수 비교"])
def test_fact_comparison_takes_precedence_over_attribute_check(deps, version, initial, question):
    deps.settings["router"]["prompt_version"] = version
    for number in ("75313-1", "75288-1"):
        add_set(deps.conn, number, "AT-AT")
    deps.model = model(initial, numbers=["75313", "75288"])
    deps.ask_fn = Mock(side_effect=fake_ask)
    deps.fact_fn = Mock()
    result = answer(question, deps=deps)
    assert (result.route, result.route_source) == (
        "compare", "rule:compare_two_sets" if initial == "fact" else "llm")
    assert not result.needs_input and result.set_nums == ["75313-1", "75288-1"]
    assert result.tables and deps.ask_fn.call_args.kwargs["injected_cards"]
    deps.fact_fn.assert_not_called()


@pytest.mark.parametrize("numbers,question,expected", [
    (["A"], "부품 수 비교", "fact"),
    (["A", "A-1"], "부품 수 비교", "fact"),
    (["A", "B"], "부품 몇 개", "fact"),
    (["A", "missing"], "부품 수 비교", "fact"),
    (["A"], "구성 차이", "narrative"),
])
def test_fact_compare_requires_cue_and_two_distinct_resolved_sets(deps, numbers, question, expected):
    from rag_service.rag.schemas import RouterOutputV2
    from rag_service.services.orchestrator import _rules

    route, source, resolved = _rules(RouterOutputV2(route="fact", set_nums=numbers, confidence=.95),
                                     question, None, False, deps, "llm")
    assert route == expected and source != "rule:compare_two_sets"
    assert bool(resolved.needs_input) == ("missing" in numbers)


@pytest.mark.parametrize("version", ["router-v2", "router-v3"])
def test_unsupported_attribute_precedes_fact_and_compare_rules(deps, version):
    deps.settings["router"]["prompt_version"] = version
    deps.model = model("fact", numbers=["75313", "75288"])
    deps.ask_fn = Mock()
    result = answer("75313하고 75288 크기랑 구성 차이", deps=deps)
    assert (result.route, result.route_source) == ("fact", "rule:unsupported_attr")
    assert not result.needs_input and "수집하지 않아 확인되지 않습니다" in result.answer
    deps.ask_fn.assert_not_called()


def test_fact_attribute_cues_are_configurable(deps):
    deps.settings["router"]["fact_cues"] = ["CUSTOM"]
    deps.model = model("fact", numbers=["A"])
    deps.ask_fn = Mock(side_effect=fake_ask)
    assert answer("custom", deps=deps).route == "fact"
    deps.model = model("fact", numbers=["A"])
    assert answer("부품 몇 개", deps=deps).route_source == "rule:fact_without_attribute"


@pytest.mark.parametrize("question", ["75192 한국 정가는?", "재고 있어?", "할인 가격", "구매처",
                                     "설명서", "무게", "크기", "MSRP", "가로 길이"])
def test_unsupported_attrs_do_not_need_subject_or_call_rag(deps, question):
    deps.model = model("narrative")
    deps.ask_fn = Mock()
    deps.fact_fn = Mock()
    result = answer(question, deps=deps)
    assert result.route == "fact" and result.route_source == "rule:unsupported_attr"
    assert not result.needs_input and not result.cards
    assert "수집하지 않아 확인되지 않습니다" in result.answer
    assert "https://www.lego.com/ko-kr" in result.answer
    deps.ask_fn.assert_not_called()
    deps.fact_fn.assert_not_called()


def test_supported_amount_question_is_not_price():
    assert unsupported_answer("내 부품으로 만들려면 얼마나 부족해?") is None


def test_unsupported_dictionary_path_is_configurable(tmp_path):
    settings = load_settings()
    path = tmp_path / "attrs.yaml"
    path.write_text('attributes:\n  - keywords: ["custom"]\n    reason: "미수집"\nalternative: "대안"\n', encoding="utf-8")
    settings["paths"]["unsupported_attrs"] = str(path)
    assert unsupported_answer("CUSTOM", settings).endswith("대안")
    assert unsupported_answer("가격", settings) is None


def test_fact_multi_attribute_and_direct_unsupported(conn):
    result = answer_fact(conn, "A", "출시 연도와 테마는?")
    assert "출시 연도" in result.answer and "서브테마" in result.answer
    assert "부품 수" not in result.answer and "미니피그(" not in result.answer
    assert "확인되지 않습니다" in answer_fact(conn, "A", "정가").answer


@pytest.mark.parametrize("question", ["미니피그 몇 개야?", "How many minifigures?"])
def test_minifigure_count_does_not_add_parts(conn, question):
    result = answer_fact(conn, "A", question)
    assert "미니피그(" in result.answer
    assert "부품 수" not in result.answer and "출시 연도" not in result.answer


def test_v3_preserves_v2_and_adds_boundaries():
    old = router_messages("질문", False, "router-v2")[0].content
    new = router_messages("질문", False, "router-v3")[0].content
    assert "경계 규칙" not in old
    assert all(text in new for text in ("경계 규칙", "캐릭터", "피규어=미니피그", "색깔 상관없이", "활약"))


@pytest.mark.parametrize("selected", [None, "A-1"])
@pytest.mark.parametrize("ref", ["그 기체", "그거", "그 세트"])
def test_build_archive_reference_uses_rank_one_without_selection_llm(deps, selected, ref):
    deps.model = model("build_archive", refs=["selected"])
    deps.collection = {"sets": [("A-1", 1)]}
    first = Recommendation(SetInfo("B-1", "First", 2018, 5, "Star Wars", "core"), 2, 1, .8, 5, False)
    second = Recommendation(SetInfo("C-1", "Second", 2019, 5, "Star Wars", "core"), 1, 1, .7, 5, False)
    deps.recommend_fn = Mock(return_value=[first, second])
    deps.ask_fn = Mock(side_effect=fake_ask)
    result = answer(f"만들 수 있는 거 추천하고 {ref}가 어떤 건지도 설명해줘", selected_set=selected, deps=deps)
    assert not result.needs_input and result.route == "build_archive"
    assert result.set_nums == ["B-1"]
    assert "selection" not in result.usage
    assert deps.ask_fn.call_args.args[0] == "B-1"


def test_build_ignore_color_mode_reaches_planner(deps):
    deps.model = model("build")
    deps.collection = {"sets": [("A-1", 1)]}
    from rag_service.rag.templates import TemplateAnswer
    deps.build_fn = Mock(return_value=TemplateAnswer("추천 없음", [], [], []))
    answer("색깔 상관없이 만들 수 있는 거 추천", deps=deps)
    assert deps.build_fn.call_args.args[2] == "ignore_color"


@pytest.mark.parametrize("mode,expected", [("none", 8), ("series_list_only", 6), ("all", 4)])
def test_mmr_caps_only_limit_requested_categories(mode, expected):
    cfg = load_settings()["retrieval"]
    cfg.update(mmr_caps=mode, top_k=10)
    candidates = [hit(f"s{i}", scope="series", doc_id="series") for i in range(4)]
    candidates += [hit(f"g{i}", kind="group", doc_id="group") for i in range(4)]
    result = _merge([], None, candidates, "A-1", cfg)
    assert len(result) == expected
    cfg["mmr_caps"] = "series_list_only"
    listed = [hit(f"l{i}", kind="list") for i in range(5)]
    assert len(_merge([], None, listed, "A-1", cfg)) == 2


def test_invalid_mmr_caps_rejected():
    with pytest.raises(ValueError, match="MMR 상한"):
        common.overrides(load_settings(), ["retrieval.mmr_caps=invalid"])


def test_retrieval_reports_series_heavy_questions():
    from rag_service.eval.retrieval import evaluate
    from test_eval import case

    candidates = [(doc.metadata, score) for doc, score in [hit(str(i), scope="series") for i in range(3)]]
    rows, summary = evaluate([case()], lambda _: candidates, load_settings())
    assert rows[0]["series_chunks"] == summary["series_chunks"] == 3
    assert summary["series_heavy_questions"] == 1


def test_eval_errors_logged_without_console_details(tmp_path, monkeypatch, capsys):
    settings = load_settings()
    path = tmp_path / "eval_error.log"
    settings["evaluation"]["error_log"] = str(path)
    monkeypatch.setattr(common, "load_settings", lambda: settings)
    def fail():
        raise RuntimeError("private-diagnostic")
    assert common.guarded(fail) == 1
    assert "RuntimeError: private-diagnostic" in path.read_text(encoding="utf-8")
    assert "private-diagnostic" not in capsys.readouterr().err
    path.unlink()
    path.mkdir()
    assert common.guarded(fail) == 1
    assert len(capsys.readouterr().err.splitlines()) == 1


def test_real_at_at_review_rejection_has_candidate(real_conn, tmp_path):
    from test_search_offline import model_for, ranked
    from rag_service.services.search import search_sets

    settings = load_settings()
    settings["paths"].update(docs_dir=str(tmp_path / "docs"), collect_sqlite=str(tmp_path / "cache.sqlite"))
    llm = model_for({"year_from": 2020, "parts_max": 500, "name_keywords": ["AT-AT"], "semantic": "AT-AT"},
                    {"keep": [], "drop": [{"id": "75298-1", "reason": "검수 제외 재현"}]})
    result = search_sets("2020년 이후 500부품 이하 AT-AT", conn=real_conn, model=llm,
                         meta_store=ranked("75298-1"), settings=settings)
    assert result.total_sql == 1 and result.no_result
    assert "후보 1개가 있었지만 검수에서 제외" in result.template.answer
    assert "75298-1" in result.template.answer
    assert result.template.tables[-1]["rows"] == [["75298-1", "AT-AT vs. Tauntaun Microfighters"]]
