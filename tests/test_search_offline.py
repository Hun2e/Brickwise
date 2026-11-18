"""가짜 LLM·로컬 Chroma로 조건 탐색 계약을 검증한다."""

import importlib.util
import json
import os
import subprocess
import sys
from unittest.mock import Mock

import pytest
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from pydantic import ValidationError

from rag_service.config import load_settings, project_root
from rag_service.data_prep.doc_store import DocStore
from rag_service.rag.prompts import filter_messages, review_messages
from rag_service.rag.schemas import Dropped, FilterQuery, ReviewResult
from rag_service.services import search_sets
from test_db_queries import add_set, conn
from test_index_items_offline import offline
from test_structured_offline import FakeModel
from rag_service.vectorstore.store import get_collection


@pytest.fixture
def settings(tmp_path):
    config = load_settings()
    config["paths"].update(docs_dir=str(tmp_path / "docs"),
                           collect_sqlite=str(tmp_path / "collect.sqlite"),
                           chroma_dir=str(tmp_path / "chroma"))
    config["llm"]["max_retries"] = 0
    return config


def test_discard_warnings_use_configured_user_labels(settings):
    from rag_service.services.search import _validate_conditions

    warnings = []
    settings["search"]["field_labels"]["parts_min"] = "최소 조각 수"
    query = FilterQuery(has_minifig=True, parts_min=100, parts_max=200, year_from=2020,
                        year_to=2025, name_keywords=["unused"], exclude_keywords=["unknown"],
                        minifig="Nobody", limit=3, sort="year")
    _, discarded = _validate_conditions(query, "우주선", [], warnings, [], settings)
    assert len(discarded) == len(warnings) == 10
    assert any("최소 조각 수" in warning for warning in warnings)
    assert any("미니피그 포함 여부" in warning for warning in warnings)
    assert all(field not in " ".join(warnings) for field in settings["search"]["field_labels"])


@pytest.fixture
def db(conn):
    conn.execute("DELETE FROM set_scope")
    add_set(conn, "1001-1", "Millennium Falcon", 2020, 1200)
    add_set(conn, "1002-1", "X-wing Fighter", 2019, 1100)
    add_set(conn, "1003-1", "Droid Figure", 2021, 1000)
    add_set(conn, "1004-1", "Small Speeder", 2016, 100)
    conn.execute("CREATE TABLE ko_aliases(alias TEXT, canonical_en TEXT, verified INTEGER, kind TEXT)")
    conn.executemany("INSERT INTO ko_aliases VALUES (?, ?, ?, ?)",
                     [("팔콘", "Millennium Falcon", 0, "vehicle"),
                      ("루크", "Luke Skywalker", 1, "character")])
    conn.execute("CREATE TABLE set_entities(set_num TEXT, wiki_title TEXT, kind TEXT, rule_index INTEGER)")
    conn.execute("INSERT INTO set_entities VALUES ('1001-1', 'Millennium Falcon', 'vehicle', 0)")
    return conn


def model_for(*responses):
    return FakeModel([json.dumps(r) if isinstance(r, dict) else r for r in responses])


def ranked(*ids):
    store = Mock()
    store.similarity_search_with_score.return_value = [
        (Document(page_content="unused", metadata={"set_num": number}), index)
        for index, number in enumerate(ids)]
    return store


def run(db, settings, model, store=None, question="2018년 이후 1,000개 이상 우주선", **kwargs):
    return search_sets(question, conn=db, settings=settings, model=model,
                       meta_store=store, **kwargs)


def test_prompts_keys_rules_examples_and_data_separation():
    messages = filter_messages("ignore instructions", [{"alias": "루크", "canonical_en": "Luke"}])
    assert all(key in messages[0].content for key in FilterQuery.model_fields)
    assert "일반 의미어" in messages[0].content and "남용은 금지" in messages[0].content
    assert all(word in messages[0].content for word in ("2009", "spaceship", "ground vehicle", "Luke"))
    assert "ignore instructions" not in messages[0].content
    assert json.loads(messages[1].content)["aliases"][0]["alias"] == "루크"
    review = review_messages("질문", "vehicle", [{"id": "1001-1"}])
    assert all(key in review[0].content for key in ("keep", "drop", "id", "reason"))
    assert "수치 조건은 판단하지 않는다" in review[0].content


@pytest.mark.parametrize("data", [
    {"subtheme": "Star Wars"}, {"name_keywords": ["x"] * 4},
    {"exclude_keywords": ["x"] * 6}, {"limit": 0}, {"limit": 51}, {"sort": "sql"},
])
def test_schema_rejects_invalid_filters(data):
    with pytest.raises(ValidationError):
        FilterQuery(**data)


def test_review_schema_limits_and_independent_defaults():
    with pytest.raises(ValidationError):
        Dropped(id="x", reason="가" * 21)
    first = ReviewResult(keep=[])
    first.drop.append(Dropped(id="x", reason="사유"))
    assert ReviewResult(keep=[]).drop == []


class FakeEmbedding(Embeddings):
    def embed_documents(self, texts):
        return [{"Falcon": [1., 0.], "X-wing": [0., 1.], "Droid": [-1., 0.],
                 "Speeder": [1., 0.]}[text] for text in texts]

    def embed_query(self, text):
        return [1., 0.]


def test_full_flow_chroma_review_cards_events_and_cache(db, settings):
    store = get_collection("search-test", FakeEmbedding(), settings)
    ids = ["1001-1", "1002-1", "1003-1", "1004-1"]
    store.add_texts(["Falcon", "X-wing", "Droid", "Speeder"],
                    metadatas=[{"set_num": number} for number in ids], ids=ids)
    model = model_for({"year_from": 2018, "parts_min": 1000, "semantic": "spaceship"},
                      {"keep": ["1002-1", "1001-1", "1004-1"],
                       "drop": [{"id": "1003-1", "reason": "피규어"}]})
    events = []
    result = run(db, settings, model, store, on_event=lambda name, data: events.append(name))
    assert events == ["extracting", "querying", "ranking", "reviewing", "done"]
    assert result.set_nums == ["1001-1", "1002-1"] and result.total_sql == 3
    assert result.reviewed == 3 and result.dropped == [Dropped(id="1003-1", reason="피규어")]
    assert result.spec.sort == "year" and result.query.sort == "relevance" and result.elapsed_ms >= 0
    assert "최종 정렬: relevance" in result.template.cards[0].text
    assert result.template.cards[0].source_type == "db"
    assert "1,200" in result.template.cards[0].text and "[근거 1]" in result.template.answer
    assert [row[0] for row in result.template.tables[0]["rows"]] == result.set_nums
    again = run(db, settings, model, store)
    assert model.calls == 2 and again.set_nums == result.set_nums
    assert all(stage["calls"] == 0 and stage["cache_hit"] for stage in again.usage.values())


def test_unmentioned_keywords_and_minifig_removed(db, settings):
    result = run(db, settings, model_for({"name_keywords": ["Star", "ship"],
                                         "exclude_keywords": ["Falcon"], "minifig": "Vader"}),
                 question="우주선 찾아줘")
    assert result.total_sql == 0 and result.no_result and len(result.discarded_conditions) == 4
    assert result.query.name_keywords == result.query.exclude_keywords == []
    assert result.query.minifig is None and result.warnings


@pytest.mark.parametrize("question", ["팔콘 들어간 세트", "fAlCoN 들어간 세트"])
def test_alias_or_literal_keyword_accepted(db, settings, question):
    result = run(db, settings, model_for({"name_keywords": ["Falcon"]}), question=question)
    assert result.set_nums == ["1001-1"] and not result.discarded_conditions


def test_minifig_alias_and_exclude_alias(db, settings):
    result = run(db, settings, model_for({"minifig": "Luke", "exclude_keywords": ["Falcon"]}),
                 question="팔콘 말고 루크 미니피그가 있는 세트")
    assert result.spec.minifig == "Luke" and result.spec.exclude_keywords == ("Falcon",)
    assert result.no_result and not result.discarded_conditions


def test_numeric_conditions_removed_without_numbers(db, settings):
    result = run(db, settings, model_for({"year_from": 2018, "year_to": 2024,
                                         "parts_min": 100, "parts_max": 1500, "limit": 1}),
                 question="작은 우주선")
    assert len(result.discarded_conditions) == 5 and result.no_result and not result.set_nums
    assert result.spec.year_from is None and result.spec.parts_max is None


@pytest.mark.parametrize("question", ["천 개 이상", "천개 이상", "일천이백 개 이상", "1,000개 이상"])
def test_korean_and_arabic_numbers_accepted(db, settings, question):
    result = run(db, settings, model_for({"parts_min": 1000}), question=question)
    assert result.total_sql == 3 and result.spec.parts_min == 1000


def test_zero_results_never_relax_and_skip_ranking(db, settings):
    store = ranked()
    model = model_for({"year_from": 2030, "parts_min": 1000, "semantic": "spaceship"})
    events = []
    result = run(db, settings, model, store, question="2030년 이후 1000개 이상 우주선",
                 on_event=lambda name, data: events.append(name))
    assert result.no_result and result.total_sql == 0 and not result.set_nums
    assert result.spec.year_from == 2030 and result.spec.parts_min == 1000
    assert "조건에 맞는 세트가 없습니다" in result.template.answer
    assert model.calls == 1 and result.reviewed == 0
    store.similarity_search_with_score.assert_not_called()
    assert events == ["extracting", "querying", "done"]


def test_no_semantic_skips_review_and_defaults_to_year(db, settings):
    store = ranked()
    model = model_for({"year_from": 2018})
    result = run(db, settings, model, store)
    assert result.set_nums == ["1002-1", "1001-1", "1003-1"]
    assert result.spec.sort == "year" and result.reviewed == 0 and model.calls == 1
    store.similarity_search_with_score.assert_not_called()


@pytest.mark.parametrize("failure", ["invalid json", TimeoutError("private-key-full-prompt")])
def test_review_failure_preserves_ranked_results(db, settings, failure):
    model = model_for({"semantic": "spaceship"}, failure)
    result = run(db, settings, model, ranked("1001-1", "1002-1"))
    assert result.set_nums == ["1001-1", "1002-1"] and not result.dropped
    assert any("검수에 실패" in warning for warning in result.warnings)
    assert "private-key" not in repr(result)


def test_extraction_failure_uses_whole_question_as_semantic(db, settings):
    store = ranked("1001-1")
    result = run(db, settings, model_for("bad json", {"keep": ["1001-1"]}), store)
    assert result.query is None and result.spec.year_from is None and result.spec.parts_min is None
    assert result.total_sql == 4 and result.set_nums == ["1001-1"]
    assert "2018년 이후" in store.similarity_search_with_score.call_args.args[0]
    assert any("추출에 실패" in w for w in result.warnings)


def test_review_ignores_outsiders_duplicates_conflicts_and_omissions(db, settings):
    result = run(db, settings, model_for({"parts_min": 1000, "semantic": "spaceship"},
                 {"keep": ["1001-1", "1001-1", "1004-1"], "drop": [
                     {"id": "1001-1", "reason": "모순"}, {"id": "1003-1", "reason": "피규어"},
                     {"id": "1003-1", "reason": "중복"}, {"id": "unknown", "reason": "외부"}]}),
                 ranked("1001-1", "1002-1", "1003-1", "1004-1"))
    assert result.set_nums == ["1001-1", "1002-1"]
    assert result.dropped == [Dropped(id="1003-1", reason="피규어")]
    assert result.total_sql == 3


@pytest.mark.parametrize("sort,order,expected", [
    ("year", "desc", ["1003-1", "1001-1"]),
    ("num_parts", "desc", ["1001-1", "1002-1"]),
    ("name", "asc", ["1003-1", "1001-1"]),
])
def test_explicit_sort_after_review_and_limit(db, settings, sort, order, expected):
    model = model_for({"semantic": "vehicle", "sort": sort, "order": order, "limit": 2},
                      {"keep": ["1001-1", "1002-1", "1003-1"]})
    result = run(db, settings, model, ranked("1002-1", "1001-1", "1003-1"),
                 question="탈것 2개 정렬해줘")
    assert result.set_nums == expected


def test_implicit_sort_is_not_allowed_to_override_relevance(db, settings):
    result = run(db, settings, model_for({"semantic": "vehicle", "sort": "year", "order": "desc"},
                 {"keep": ["1001-1", "1002-1"]}), ranked("1001-1", "1002-1"), question="탈것")
    assert result.set_nums == ["1001-1", "1002-1"] and result.query.sort == "relevance"


def test_limits_scopes_and_empty_meta_index(db, settings):
    settings["search"].update(sql_candidate_limit=3, review_top_n=2, result_limit=1)
    store = ranked()
    result = run(db, settings, model_for({"semantic": "vehicle"}), store)
    call = store.similarity_search_with_score.call_args
    assert call.kwargs == {"k": 2, "filter": {"set_num": {"$in": ["1004-1", "1002-1", "1001-1"]}}}
    assert result.no_result and result.total_sql == 4 and result.reviewed == 0
    assert any("색인" in w for w in result.warnings)
    settings["search"]["default_scopes"] = []
    assert run(db, settings, model_for({"year_from": 2018})).total_sql == 0


def test_drop_all_is_no_result(db, settings):
    result = run(db, settings, model_for({"semantic": "vehicle"},
                 {"keep": [], "drop": [{"id": "1003-1", "reason": "피규어"}]}), ranked("1003-1"))
    assert result.no_result and len(result.dropped) == 1 and result.total_sql == 4
    assert "후보 1개가 있었지만 검수에서 제외" in result.template.answer
    assert "1003-1 Droid Figure" in result.template.answer
    assert result.template.tables[-1]["rows"] == [["1003-1", "Droid Figure"]]
    assert "조건에 맞는 세트가 없습니다" not in result.template.answer


def test_corrupt_cache_retried_and_model_setting_invalidates_cache(db, settings):
    model = model_for({}, {}, {})
    run(db, settings, model)
    cache = DocStore(settings["paths"]["docs_dir"], settings["paths"]["collect_sqlite"])
    import sqlite3
    with sqlite3.connect(cache.collect_sqlite) as connection:
        connection.execute("UPDATE llm_cache SET response_json='invalid'")
    run(db, settings, model)
    settings["llm"]["chat_model"] = "other-fake-model"
    run(db, settings, model)
    assert model.calls == 3


def test_cli_help_and_missing_key():
    env = dict(os.environ, PYTHON_DOTENV_DISABLED="1", PYTHONIOENCODING="utf-8")
    env.pop("GMS_KEY", None)
    bootstrap = """import sys, runpy
def audit(event, args):
    if event in ('socket.connect', 'socket.getaddrinfo'):
        raise AssertionError('network forbidden')
    if event == 'open' and str(args[0]).replace('\\\\', '/').split('/')[-1] == '.env':
        raise AssertionError('env access forbidden')
sys.addaudithook(audit)
runpy.run_path('scripts/search_sets.py', run_name='__main__')
"""
    for args, code, expected in [(["--help"], 0, "--question"),
                                  (["--question", "우주선"], 2, "GMS_KEY")]:
        result = subprocess.run([sys.executable, "-c", bootstrap, *args], cwd=project_root(),
                                 env=env, capture_output=True, text=True, encoding="utf-8")
        assert result.returncode == code, result.stderr
        assert expected in result.stdout and "Traceback" not in result.stdout + result.stderr


def test_cli_serializes_query_and_dropped(db, settings, monkeypatch, capsys):
    module_spec = importlib.util.spec_from_file_location("search_cli", project_root() / "scripts/search_sets.py")
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    result = run(db, settings, model_for({"semantic": "vehicle"},
                 {"keep": [], "drop": [{"id": "1003-1", "reason": "피규어"}]}), ranked("1003-1"))
    capsys.readouterr()
    monkeypatch.setattr(module, "load_settings", lambda: settings)
    monkeypatch.setattr(module, "get_chat_model", lambda _: object())
    monkeypatch.setattr(module, "get_embeddings", lambda _: FakeEmbedding())
    monkeypatch.setattr("rag_service.vectorstore.embed_cache.CachedEmbeddings", lambda *a: FakeEmbedding())
    monkeypatch.setattr("rag_service.vectorstore.store.get_collection", lambda *a: object())
    monkeypatch.setattr(module.sqlite3, "connect", lambda *a, **k: Mock())
    monkeypatch.setattr(module, "search_sets", lambda *a, **k: result)
    assert module.main(["--question", "탈것"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["query"]["semantic"] == "vehicle"
    assert output["dropped"] == [{"id": "1003-1", "reason": "피규어"}]


@pytest.mark.parametrize("has_minifig,question,expected", [
    (False, "미니피그 없는 세트", ["1004-1", "1002-1", "1003-1"]),
    (True, "피규어 있는 세트", ["1001-1"]),
])
def test_has_minifig_reaches_sql(db, settings, has_minifig, question, expected):
    db.execute("INSERT INTO minifigs VALUES ('feedback-fig', 'Luke Skywalker', 4, '')")
    db.execute("INSERT INTO inventories VALUES (9900, 1, '1001-1')")
    db.execute("INSERT INTO inventory_minifigs VALUES (9900, 'feedback-fig', 1)")
    result = run(db, settings, model_for({"has_minifig": has_minifig}), question=question)
    assert result.spec.has_minifig is has_minifig
    assert result.set_nums == expected and not result.discarded_conditions
    assert result.reviewed == 0


@pytest.mark.parametrize("value", [True, False])
def test_has_minifig_without_mention_is_discarded(db, settings, value):
    result = run(db, settings, model_for({"has_minifig": value}), question="세트 찾아줘")
    assert result.query.has_minifig is None and result.spec.has_minifig is None
    assert result.no_result and result.discarded_conditions == [f"has_minifig={value}"]


def test_has_minifig_false_and_semantic_uses_review(db, settings):
    store = ranked("1004-1")
    model = model_for({"has_minifig": False, "semantic": "small set"}, {"keep": ["1004-1"]})
    result = run(db, settings, model, store, question="미니피그 없는 소형 세트")
    assert result.spec.has_minifig is False and result.set_nums == ["1004-1"]
    assert result.reviewed == 1 and model.calls == 2


@pytest.mark.parametrize("word,english,field", [
    ("배틀팩", "Battle Pack", "exclude_keywords"),
    ("배틀 팩", "Battle Pack", "exclude_keywords"),
    ("헬멧", "Helmet", "name_keywords"),
    ("마이크로파이터", "Microfighter", "exclude_keywords"),
    ("미니 빌드", "Mini-Build", "name_keywords"),
    ("20주년", "20th Anniversary", "name_keywords"),
    ("타이", "TIE", "name_keywords"),
])
def test_pm_product_terms_support_keywords_without_db_aliases(db, settings, word, english, field):
    db.execute("DROP TABLE ko_aliases")
    path = project_root() / settings["paths"]["ko_terms"]
    before = path.read_bytes()
    add_set(db, "2001-1", english, 2018, 200)
    result = run(db, settings, model_for({field: [english]}), question=f"{word}를 검색 조건에 적용")
    assert not result.discarded_conditions and getattr(result.spec, field) == (english,)
    assert ("2001-1" in result.set_nums) is (field == "name_keywords")
    assert path.read_bytes() == before


def test_product_terms_do_not_justify_minifig_or_unmentioned_terms(db, settings):
    result = run(db, settings, model_for({"minifig": "Helmet", "exclude_keywords": ["Battle Pack"]}),
                 question="헬멧")
    assert result.no_result and len(result.discarded_conditions) == 2


def test_terms_path_and_prompt_hints_follow_settings(db, settings, tmp_path):
    terms_path = tmp_path / "test-terms.yaml"
    terms_path.write_text('terms:\n  시험용어: ["Falcon"]\n', encoding="utf-8")
    settings["paths"]["ko_terms"] = str(terms_path)
    result = run(db, settings, model_for({"name_keywords": ["Falcon"]}), question="시험용어가 있는 세트")
    assert result.set_nums == ["1001-1"]
    hints = [{"alias": "시험용어", "canonical_en": "Falcon"}]
    assert json.loads(filter_messages("시험용어", terms=hints)[1].content)["terms"] == hints


@pytest.mark.parametrize("particle", ["", "가", "이", "는", "은", "를", "을", "의", "와", "과",
                                     "로", "으로", "에", "에서", "도", "만"])
def test_partial_alias_with_particles(db, settings, particle):
    db.execute("UPDATE ko_aliases SET alias='루크 스카이워커', verified=0 WHERE alias='루크'")
    result = run(db, settings, model_for({"minifig": "Luke"}), question=f"루크{particle} 들어 있는 세트")
    assert result.spec.minifig == "Luke" and not result.discarded_conditions


@pytest.mark.parametrize("question,accepted", [
    ("스카이워커가 나오는 세트", True), ("루크 스카이워커의 세트", True),
    ("루크가짜 세트", False), ("가짜루크 세트", False), ("루크스카이워커 세트", False),
])
def test_alias_components_use_whole_tokens(db, settings, question, accepted):
    db.execute("UPDATE ko_aliases SET alias='루크 스카이워커' WHERE alias='루크'")
    result = run(db, settings, model_for({"minifig": "Luke"}), question=question)
    assert (result.spec.minifig == "Luke") is accepted
    assert bool(result.discarded_conditions) is not accepted


@pytest.mark.parametrize("response", [{}, {"semantic": " "}, {"minifig": "Vader"},
                                       {"sort": "year", "limit": 3}])
def test_no_understood_condition_never_queries_all_sets(db, settings, monkeypatch, response):
    sql = Mock(side_effect=AssertionError("SQL must not run without conditions"))
    monkeypatch.setattr("rag_service.services.search.filter_sets", sql)
    events = []
    result = run(db, settings, model_for(response), question="정렬해서 3개 보여줘",
                 on_event=lambda name, data: events.append(name))
    assert result.no_result and result.set_nums == [] and result.total_sql == 0
    assert result.template.answer == "조건을 이해하지 못했습니다. 연도·부품 수·캐릭터·세트 종류로 다시 물어 주세요"
    assert not result.template.cards and not result.template.tables
    assert events == ["extracting", "done"]
    sql.assert_not_called()


def test_semantic_only_condition_remains_valid(db, settings):
    model = model_for({"semantic": "spaceship"}, {"keep": ["1001-1"]})
    result = run(db, settings, model, ranked("1001-1"), question="우주선")
    assert result.set_nums == ["1001-1"] and result.reviewed == 1 and model.calls == 2


@pytest.mark.parametrize("phrase,query,expected", [
    ("1,000개 넘는", {"parts_min": 1001}, ["1002-1", "1001-1"]),
    ("1,000개 초과", {"parts_min": 1001}, ["1002-1", "1001-1"]),
    ("1,000개보다 많은", {"parts_min": 1001}, ["1002-1", "1001-1"]),
    ("1,000개 이상", {"parts_min": 1000}, ["1002-1", "1001-1", "1003-1"]),
    ("1,000개부터", {"parts_min": 1000}, ["1002-1", "1001-1", "1003-1"]),
    ("1,000개 미만", {"parts_max": 999}, ["1004-1"]),
    ("1,000개보다 적은", {"parts_max": 999}, ["1004-1"]),
    ("1,000개 이하", {"parts_max": 1000}, ["1004-1", "1003-1"]),
])
def test_integer_bounds_from_extraction_are_preserved(db, settings, phrase, query, expected):
    result = run(db, settings, model_for(query), question=f"{phrase} 세트")
    assert result.set_nums == expected and not result.discarded_conditions


def test_feedback_prompt_rules_and_review_examples_are_valid():
    prompt = filter_messages("조건")[0].content
    assert all(word in prompt for word in ("has_minifig", '"has_minifig":false', "넘는", "초과",
                                           "보다 많은", '"parts_min":1501', '"parts_min":1500',
                                           "보다 적은", '"parts_max":499', '"parts_max":500'))
    prompt = review_messages("지상 탈것", "ground vehicle", [])[0].content
    decoder = json.JSONDecoder()
    examples = []
    for line in prompt.splitlines():
        if line.startswith('{"keep"'):
            value, _ = decoder.raw_decode(line)
            examples.append(ReviewResult.model_validate(value))
    assert len(examples) == 2
    assert examples[0].keep == ["a"] and len(examples[0].drop) == 1
    assert not examples[1].keep and len(examples[1].drop) == 2


@pytest.mark.parametrize("bad_response", [
    "invalid json", {"keep": [], "drop": [{"id": "1003-1", "reason": "가" * 21}]},
])
def test_review_parsing_failure_reproduces_two_attempts(db, settings, bad_response):
    settings["llm"]["max_retries"] = 1
    model = model_for({"semantic": "vehicle"}, bad_response,
                      {"keep": [], "drop": [{"id": "1003-1", "reason": "피규어"}]})
    result = run(db, settings, model, ranked("1003-1"), question="탈것")
    assert result.usage["review"]["calls"] == 2 and model.calls == 3
    assert result.dropped == [Dropped(id="1003-1", reason="피규어")]
    again = run(db, settings, model, ranked("1003-1"), question="탈것")
    assert again.usage["review"]["calls"] == 0 and model.calls == 3


@pytest.fixture
def vehicle_db(db):
    db.executemany("INSERT INTO ko_aliases VALUES (?, ?, ?, ?)", [
        ("스타 디스트로이어", "Star Destroyer", 1, "vehicle"),
        ("데스 스타", "Death Star", 1, "vehicle"),
        ("밀레니엄 팰컨", "Millennium Falcon", 1, "vehicle"),
        ("밀레니엄 팔콘", "Millennium Falcon", 0, "vehicle"),
        ("엑스윙", "X-wing fighter", 0, "vehicle"),
        ("타이파이터", "TIE fighter", 0, "vehicle"),
    ])
    add_set(db, "3001-1", "Imperial Star Destroyer", 2022, 1600)
    add_set(db, "3002-1", "TIE Interceptor", 2023, 400)
    add_set(db, "3003-1", "AT-AT", 2024, 1300)
    add_set(db, "3004-1", "AT-ST", 2025, 700)
    add_set(db, "3005-1", "Millennium Falcon", 2017, 7500)
    return db


@pytest.mark.parametrize("question,response,keyword,expected", [
    ("최신순으로 X-wing 세트 보여줘", {"sort": "year", "order": "desc"}, "X-wing", ["1002-1"]),
    ("1,500개 넘는 스타 디스트로이어", {"parts_min": 1501}, "Star Destroyer", ["3001-1"]),
    ("500개 이하 TIE 세트", {"parts_max": 500}, "TIE", ["3002-1"]),
])
def test_feedback2_q5_q8_q9_missing_llm_keywords(vehicle_db, settings, question, response, keyword, expected):
    model = model_for(response)
    result = run(vehicle_db, settings, model, question=question)
    assert result.query.name_keywords == [keyword]
    assert result.spec.name_keywords == (keyword,)
    assert result.added_conditions == [f"name_keywords={keyword}"]
    assert result.set_nums == expected and result.total_sql == 1
    again = run(vehicle_db, settings, model, question=question)
    assert again.added_conditions == result.added_conditions and model.calls == 1


def test_feedback2_q1_semantic_does_not_invent_keywords(vehicle_db, settings):
    model = model_for({"year_from": 2018, "parts_min": 1000, "semantic": "spaceship"},
                      {"keep": ["1001-1"]})
    result = run(vehicle_db, settings, model, ranked("1001-1"))
    assert result.added_conditions == [] and result.query.name_keywords == []
    assert result.spec.name_keywords == () and result.set_nums == ["1001-1"]
    assert result.reviewed == 1


@pytest.mark.parametrize("question,keyword", [
    ("엑스윙", "X-wing"), ("타이", "TIE"), ("에이티에이티", "AT-AT"),
    ("밀레니엄 팰컨", "Millennium Falcon"), ("밀레니엄 팔콘", "Millennium Falcon"),
    ("팔콘", "Millennium Falcon"), ("타이파이터", "TIE"),
    ("AT-ST", "AT-ST"), ("AT-AT", "AT-AT"), ("Star Destroyer", "Star Destroyer"),
])
def test_vehicle_spellings_not_wiki_titles(vehicle_db, settings, question, keyword):
    result = run(vehicle_db, settings, model_for({}), question=f"{question} 세트")
    assert result.spec.name_keywords == (keyword,)
    assert result.added_conditions == [f"name_keywords={keyword}"]
    assert result.set_nums and "fighter" not in result.spec.name_keywords[0]


@pytest.mark.parametrize("question", ["배틀팩", "헬멧", "워커", "스피더", "루크", "우주선", "탈것", "작은"])
def test_product_character_and_general_words_are_not_added(vehicle_db, settings, question):
    result = run(vehicle_db, settings, model_for({}), question=f"{question} 세트")
    assert result.added_conditions == [] and result.no_result
    assert result.spec.name_keywords == ()


def test_model_keyword_dedup_and_exclusion_are_preserved(vehicle_db, settings):
    included = run(vehicle_db, settings, model_for({"name_keywords": ["x-WING"]}),
                   question="X-wing 세트")
    assert included.spec.name_keywords == ("x-WING",) and not included.added_conditions
    excluded = run(vehicle_db, settings, model_for({"exclude_keywords": ["X-wing"]}),
                   question="X-wing 말고 세트")
    assert excluded.spec.name_keywords == () and excluded.spec.exclude_keywords == ("X-wing",)
    assert "1002-1" not in excluded.set_nums and not excluded.added_conditions


@pytest.mark.parametrize("question", ["frontier 세트", "x-winged 세트", "antiem 세트"])
def test_english_rule_boundaries_do_not_match_other_words(vehicle_db, settings, question):
    result = run(vehicle_db, settings, model_for({}), question=question)
    assert not result.added_conditions and result.no_result


def test_rules_path_is_used_and_nonvehicle_rules_are_ignored(db, settings, tmp_path):
    path = tmp_path / "custom-rules.yaml"
    path.write_text("include_list_pages: false\nrules:\n"
                    "  - {kind: vehicle, pattern: 'testcraft', wiki_title: 'Testcraft'}\n"
                    "  - {kind: character, pattern: 'figure', wiki_title: 'Figure'}\n", encoding="utf-8")
    settings["paths"]["entity_rules"] = str(path)
    result = run(db, settings, model_for({}), question="testcraft figure")
    assert result.spec.name_keywords == ("testcraft",) and result.total_sql == 0


def test_feedback2_versions_and_shape_prompt(settings):
    assert settings["search"]["filter_prompt_version"] == "filter-v2"
    assert settings["search"]["review_prompt_version"] == "review-v2"
    prompt = filter_messages("조건")[0].content
    assert prompt.startswith("버전: filter-v2")
    for example in ('{"name_keywords":["X-wing"],"sort":"year","order":"desc"}',
                    '{"parts_min":1501,"name_keywords":["Star Destroyer"]}',
                    '{"parts_max":500,"name_keywords":["TIE"]}'):
        assert example in prompt
    assert "명시했을 때만" not in prompt
    review = review_messages("탈것", "vehicle", [])[0].content
    assert review.startswith("버전: review-v2")
    assert all(word in review for word in ("buildable figure", "흉상", "헬멧", "디오라마", "배틀팩 인물 세트",
                                           "Range Trooper", "General Grievous"))


def test_feedback2_added_conditions_in_cli_json(vehicle_db, settings, monkeypatch, capsys):
    module_spec = importlib.util.spec_from_file_location("search_cli_v2", project_root() / "scripts/search_sets.py")
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    result = run(vehicle_db, settings, model_for({"parts_max": 500}), question="500개 이하 TIE 세트")
    monkeypatch.setattr(module, "load_settings", lambda: settings)
    monkeypatch.setattr(module, "get_chat_model", lambda _: object())
    monkeypatch.setattr(module, "get_embeddings", lambda _: FakeEmbedding())
    monkeypatch.setattr("rag_service.vectorstore.embed_cache.CachedEmbeddings", lambda *a: FakeEmbedding())
    monkeypatch.setattr("rag_service.vectorstore.store.get_collection", lambda *a: object())
    monkeypatch.setattr(module.sqlite3, "connect", lambda *a, **k: Mock())
    monkeypatch.setattr(module, "search_sets", lambda *a, **k: result)
    capsys.readouterr()
    assert module.main(["--question", "500개 이하 TIE 세트"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["added_conditions"] == ["name_keywords=TIE"]
    assert output["set_nums"] == ["3002-1"]


def test_feedback3_q8_discards_death_star_only(vehicle_db, settings):
    model = model_for({"parts_min": 1501, "name_keywords": ["Star Destroyer", "Death Star"]})
    result = run(vehicle_db, settings, model, question="1,500개 넘는 스타 디스트로이어")
    assert result.query.name_keywords == ["Star Destroyer"]
    assert result.spec.parts_min == 1501 and result.spec.name_keywords == ("Star Destroyer",)
    assert result.discarded_conditions == ["name_keywords=Death Star"]
    assert result.added_conditions == [] and result.set_nums == ["3001-1"]
    assert result.total_sql == 1 and not result.no_result
    again = run(vehicle_db, settings, model, question="1,500개 넘는 스타 디스트로이어")
    assert again.discarded_conditions == result.discarded_conditions and model.calls == 1


@pytest.fixture
def ambiguous_alias_db(db):
    db.execute("UPDATE ko_aliases SET alias='루크 스카이워커' WHERE alias='루크'")
    db.executemany("INSERT INTO ko_aliases VALUES (?, ?, ?, ?)", [
        ("다스 베이더", "Darth Vader", 1, "character"),
        ("다스 몰", "Darth Maul", 1, "character"),
        ("보바 펫", "Boba Fett", 1, "character"),
        ("장고 펫", "Jango Fett", 0, "character"),
    ])
    return db


@pytest.mark.parametrize("question,minifig,accepted", [
    ("다스 베이더가 나오는 세트", "Darth Vader", True),
    ("다스 베이더가 나오는 세트", "Darth Maul", False),
    ("다스가 나오는 세트", "Darth Vader", False),
    ("다스가 나오는 세트", "Darth Maul", False),
    ("루크가 미니피그로 들어 있는 세트", "Luke Skywalker", True),
    ("보바 펫이 있는 세트", "Boba Fett", True),
    ("보바 펫이 있는 세트", "Jango Fett", False),
    ("펫이 있는 세트", "Boba Fett", False),
    ("펫이 있는 세트", "Jango Fett", False),
])
def test_feedback3_full_alias_or_unique_component(ambiguous_alias_db, settings, question, minifig, accepted):
    result = run(ambiguous_alias_db, settings, model_for({"minifig": minifig}), question=question)
    assert result.spec.minifig == (minifig if accepted else None)
    assert result.discarded_conditions == ([] if accepted else [f"minifig={minifig}"])


@pytest.mark.parametrize("field", ["name_keywords", "exclude_keywords"])
def test_feedback3_both_keyword_lists_reject_ambiguous_aliases(ambiguous_alias_db, settings, field):
    result = run(ambiguous_alias_db, settings, model_for({field: ["Darth Vader", "Darth Maul"]}),
                 question="다스 베이더 세트")
    assert getattr(result.spec, field) == ("Darth Vader",)
    assert result.discarded_conditions == [f"{field}=Darth Maul"]


@pytest.mark.parametrize("alias,question", [
    ("요다 (스타워즈)", "스타워즈 세트"),
    ("요다 (먼 은하)", "은하 세트"),
    ("요다（스타워즈）", "스타워즈 세트"),
])
def test_parenthetical_components_are_not_used_even_when_unique(db, settings, alias, question):
    db.execute("INSERT INTO ko_aliases VALUES (?, 'Yoda', 1, 'character')", (alias,))
    result = run(db, settings, model_for({"minifig": "Yoda"}), question=question)
    assert result.spec.minifig is None and result.discarded_conditions == ["minifig=Yoda"]


def test_full_alias_with_parentheses_is_still_accepted(db, settings):
    db.execute("INSERT INTO ko_aliases VALUES ('요다 (스타워즈)', 'Yoda', 1, 'character')")
    result = run(db, settings, model_for({"minifig": "Yoda"}), question="요다 (스타워즈) 세트")
    assert result.spec.minifig == "Yoda" and not result.discarded_conditions


def test_component_uniqueness_counts_canonical_names_not_alias_rows(db, settings):
    db.execute("UPDATE ko_aliases SET alias='루크 스카이워커' WHERE alias='루크'")
    db.execute("INSERT INTO ko_aliases VALUES ('루크 제다이', 'Luke Skywalker', 0, 'character')")
    result = run(db, settings, model_for({"minifig": "Luke"}), question="루크가 있는 세트")
    assert result.spec.minifig == "Luke" and not result.discarded_conditions


def test_component_uniqueness_recomputed_after_alias_data_changes(db, settings):
    db.execute("INSERT INTO ko_aliases VALUES ('가상 기사', 'First Character', 1, 'character')")
    model = model_for({"minifig": "First Character"}, {"minifig": "First Character"})
    first = run(db, settings, model, question="가상이 있는 세트")
    assert first.spec.minifig == "First Character"
    db.execute("INSERT INTO ko_aliases VALUES ('가상 조종사', 'Second Character', 0, 'character')")
    second = run(db, settings, model, question="가상이 있는 세트")
    assert second.spec.minifig is None and second.discarded_conditions == ["minifig=First Character"]
    assert model.calls == 2


@pytest.mark.parametrize("question,response,expected", [
    ("다스 베이더와 Darth Maul 세트", {"name_keywords": ["Darth Maul"]}, ("Darth Maul",)),
    ("다스 베이더 말고 배틀팩", {"exclude_keywords": ["Battle Pack"]}, ("Battle Pack",)),
])
def test_literal_and_product_terms_do_not_require_unique_aliases(
    ambiguous_alias_db, settings, question, response, expected,
):
    result = run(ambiguous_alias_db, settings, model_for(response), question=question)
    assert getattr(result.spec, next(iter(response))) == expected
    assert not result.discarded_conditions
