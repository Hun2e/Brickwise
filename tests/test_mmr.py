"""MMR·이중 질의·고정 캐시·실험 행렬을 네트워크 없이 검증한다."""

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from test_index_items_offline import cached, offline
from test_retrieve_offline import deps, hit, run
from test_eval import case, script, settings
from rag_service.eval.common import overrides, write_jsonl
from rag_service.eval.experiments import (MissingQueryEmbedding, QueryEmbeddings, decision, grid_markdown,
                                         measure, read_grid, run_grid, variants, winner)
from rag_service.eval.retrieval import frozen_terms
from rag_service.rag.retrieve import _distance, rank_docs, search_queries


@pytest.mark.parametrize("apply_caps,count", [(True, 2), (False, 5)])
def test_mmr_settings_and_service_caps(deps, apply_caps, count):
    deps.settings["retrieval"].update(search_type="mmr", mmr_apply_caps=apply_caps,
                                      mmr_fetch_k=40, mmr_lambda=.3)
    store = deps.docs_store
    store.embeddings.embed_query.return_value = [1., 0.]
    docs = [hit(str(index), doc_id="same")[0] for index in range(5)]
    store.max_marginal_relevance_search_by_vector.return_value = docs
    store.get.return_value = {"ids": [doc.id for doc in reversed(docs)],
                              "embeddings": [[float(i), 1.] for i in reversed(range(5))]}
    store._collection.configuration = {"hnsw": {"space": "l2"}}
    cards = run(deps)["cards"]
    assert len(cards) == count + 1 and cards[0].source_type == "set_meta"
    assert [card.score for card in cards[1:]] == [2., 1., 2., 5., 10.][:count]
    store.max_marginal_relevance_search_by_vector.assert_called_once_with(
        [1., 0.], k=5, fetch_k=40, lambda_mult=.3,
        filter={"$or": [{"sets": {"$contains": "75076-1"}}, {"scope": "series"}]})
    store.similarity_search_with_score.assert_not_called()


@pytest.mark.parametrize("space,expected", [("l2", 2.), ("cosine", 1.), ("ip", 1.)])
def test_distance_metric(space, expected):
    store = SimpleNamespace(_collection=SimpleNamespace(configuration={"hnsw": {"space": space}}, metadata=None))
    assert _distance(store, [1., 0.], [0., 1.]) == pytest.approx(expected)


def test_empty_mmr_and_zero_top_k(deps):
    deps.settings["retrieval"].update(search_type="mmr")
    deps.docs_store.max_marginal_relevance_search_by_vector.return_value = []
    assert len(run(deps)["cards"]) == 1
    deps.docs_store.get.assert_not_called()
    deps.docs_store.reset_mock()
    deps.settings["retrieval"]["top_k"] = 0
    assert len(run(deps)["cards"]) == 1
    deps.docs_store.embeddings.embed_query.assert_not_called()


def test_dual_similarity_union_minimum_distance_and_no_backfill(deps):
    deps.settings["retrieval"].update(query_mode="dual", similarity_fetch_k=40)
    deps.docs_store.similarity_search_with_score.side_effect = [
        [hit("a", score=.8), hit("b", score=.4)], [hit("a", score=.1), hit("c", score=.2)]]
    cards = run(deps)["cards"][1:]
    assert [(card.chunk_id, card.score) for card in cards] == [("a", .1), ("c", .2), ("b", .4)]
    assert [call.args[0] for call in deps.docs_store.similarity_search_with_score.call_args_list] == [
        "역할?", "역할? Gunship battle"]
    assert all(call.kwargs["k"] == 40 for call in deps.docs_store.similarity_search_with_score.call_args_list)


def test_dual_mmr_unions_before_selection(deps):
    deps.settings["retrieval"].update(search_type="mmr", query_mode="dual", top_k=2,
                                      mmr_lambda=0., mmr_apply_caps=False)
    store = deps.docs_store
    store.embeddings.embed_query.side_effect = [[1., 0.], [0., 1.]]
    store.similarity_search_by_vector_with_relevance_scores.side_effect = [
        [hit("a", score=.2), hit("b", score=.3)], [hit("a", score=.1), hit("c", score=.4)]]
    store.get.return_value = {"ids": ["c", "a", "b"], "embeddings": [[0., 1.], [1., 0.], [.99, .01]]}
    cards = run(deps)["cards"][1:]
    assert [(card.chunk_id, card.score) for card in cards] == [("a", .1), ("c", .4)]
    assert store.similarity_search_by_vector_with_relevance_scores.call_count == 2
    store.max_marginal_relevance_search_by_vector.assert_not_called()


def test_query_modes_no_duplicate_query(settings):
    cfg = settings["retrieval"]
    assert search_queries("q", cfg, [" en "], None) == ["q en"]
    cfg["query_mode"] = "question"
    assert search_queries("q", cfg, [" en "], None) == ["q"]
    cfg["query_mode"] = "dual"
    assert search_queries("q", cfg, [], None) == ["q"]


@pytest.mark.parametrize("mode", ["expanded", "dual"])
def test_real_chroma_mmr_filter_distances_and_no_document_embedding(tmp_path, deps, mode):
    from rag_service.vectorstore.store import get_collection

    embeddings = cached(tmp_path)
    deps.settings["paths"]["chroma_dir"] = str(tmp_path / "chroma")
    deps.docs_store = get_collection("mmr-docs", embeddings, deps.settings)
    deps.docs_store.add_documents([hit("a", sets=["75076-1"])[0], hit("b", scope="series")[0],
                                   hit("outside", sets=["other"])[0]])
    deps.settings["retrieval"].update(search_type="mmr", query_mode=mode)
    vectors = Mock()
    vectors.embed_documents.side_effect = AssertionError("document re-embedding")
    # 서비스 질의 문자열만 캐시에 준비한다.
    queries = search_queries("역할?", deps.settings["retrieval"], ["Gunship", "battle"], None)
    for query in queries:
        embeddings.embed_query(query)
    embeddings.inner = vectors
    cards = run(deps)["cards"][1:]
    assert {card.chunk_id for card in cards} == {"a", "b"}
    expected = {}
    for query in queries:
        for doc, score in deps.docs_store.similarity_search_with_score(query, k=10):
            expected[doc.id] = min(expected.get(doc.id, float("inf")), score)
    assert all(card.score == pytest.approx(expected[card.chunk_id], rel=1e-5) for card in cards)
    vectors.embed_documents.assert_not_called()


@pytest.mark.parametrize("value", ["retrieval.query_mode=bad", "retrieval.mmr_lambda=-0.1",
                                   "retrieval.mmr_lambda=1.1", "retrieval.mmr_fetch_k=0",
                                   "retrieval.similarity_fetch_k=-1", "retrieval.mmr_apply_caps=1"])
def test_invalid_experiment_settings(settings, value):
    with pytest.raises(ValueError):
        overrides(settings, [value])


def test_fixed_cache_miss_never_calls_llm(settings, tmp_path):
    path = tmp_path / "missing.jsonl"
    with pytest.raises(ValueError, match="고정 P1 캐시"):
        frozen_terms(case(), "Title", path=path, settings=settings)
    assert not path.exists()


def test_query_cache_read_only_and_miss(settings, tmp_path):
    embeddings = cached(tmp_path, settings["llm"]["embedding_model"])
    vector = embeddings.embed_query("cached")
    settings["paths"]["embed_cache"] = str(embeddings.cache_path)
    before = embeddings.cache_path.read_bytes()
    reader = QueryEmbeddings(settings)
    assert reader.embed_query("cached") == reader.embed_query("cached") == vector
    with pytest.raises(MissingQueryEmbedding):
        reader.embed_query("missing")
    with pytest.raises(ValueError):
        reader.embed_documents(["document"])
    assert embeddings.cache_path.read_bytes() == before


def result(settings, *, hits=26, recall=.65, latency=10., ko=.54, en=.86, name="reference"):
    return {"name": name, "settings": deepcopy(settings), "summary": {
        "hit_questions": hits, "primary_metric": "recall@5", "median_retrieval_ms": latency,
        "overall": {"metrics": {"recall@5": recall}},
        "lang": {lang: {"metrics": {"recall@5": score}} for lang, score in (("ko", ko), ("en", en))}}}


@pytest.mark.parametrize("changes,passed", [({"hits": 28, "latency": 12.}, True),
    ({"hits": 27}, False), ({"hits": 28, "latency": 12.1}, False),
    ({"hits": 28, "ko": .53}, False), ({"hits": 28, "en": .85}, False)])
def test_decision_all_constraints(settings, changes, passed):
    policy = read_grid("experiments/t017.yaml")["policy"]
    assert decision(result(settings, **changes), result(settings), policy)["passed"] is passed


def test_grid_stages_fallback_and_simpler_tie(settings):
    grid = read_grid("experiments/t017.yaml")
    original = deepcopy(settings)
    calls = []
    def run(name, config):
        calls.append((name, deepcopy(config)))
        improved = config["retrieval"]["similarity_fetch_k"] == 40 or config["retrieval"]["search_type"] == "mmr"
        return result(config, name=name, hits=28 if improved else 26, recall=.7 if improved else .65)
    results, selected = run_grid(settings, grid, run)
    assert len(results) == 22 and settings == original
    assert selected["E1+E2"] == "e1-02"
    assert all(config["retrieval"]["similarity_fetch_k"] == 40 for name, config in calls if name.startswith("e3"))
    assert len({config["retrieval"]["query_mode"] for name, config in calls if name.startswith("e3")}) == 3
    assert {config["retrieval"]["top_k"] for name, config in calls if name.startswith("e4")} == {3, 5, 8}
    no_gain, selected = run_grid(settings, grid, lambda name, cfg: result(cfg, name=name))
    assert set(selected.values()) == {"reference"}
    assert all(not row["decision"]["passed"] for row in no_gain)


@pytest.mark.parametrize("missing_queries", [False, True])
def test_grid_cli_fake_stores_no_network(settings, tmp_path, monkeypatch, missing_queries):
    from test_collect_offline import FakeModel

    settings["evaluation"].update(retrieval_file=str(tmp_path / "cases.jsonl"),
                                   en_terms_cache=str(tmp_path / "terms.jsonl"), repetitions=1)
    cases = [case(), case(id="R-2", lang="en")]
    write_jsonl(Path(settings["evaluation"]["retrieval_file"]), cases)
    for row in cases:
        frozen_terms(row, "Title", path=Path(settings["evaluation"]["en_terms_cache"]), settings=settings,
                     model=FakeModel([{"queries": [], "en_terms": []}]))
    meta = Mock()
    meta.get.return_value = {"ids": ["12345-1"], "documents": ["meta"], "metadatas": [{"name": "Title"}]}
    docs = Mock()
    def similarity(query, **kwargs):
        if missing_queries and query == case()["question"]:
            raise MissingQueryEmbedding("질의 벡터 캐시가 없습니다.")
        return [hit("a", doc_id="en:a")]
    docs.similarity_search_with_score.side_effect = similarity
    docs.embeddings.embed_query.return_value = [1., 0.]
    docs.max_marginal_relevance_search_by_vector.return_value = [hit("a", doc_id="en:a")[0]]
    docs.get.return_value = {"ids": ["a"], "embeddings": [[1., 0.]]}
    docs._collection.configuration = {"hnsw": {"space": "l2"}}
    docs.similarity_search_by_vector_with_relevance_scores.return_value = [hit("a", doc_id="en:a")]
    cli = script("eval_retrieval")
    monkeypatch.setattr(cli, "configured", lambda args: settings)
    from contextlib import nullcontext
    monkeypatch.setattr(cli, "temporary_snapshot", lambda *args: nullcontext((object(), {"name": "fake"})))
    monkeypatch.setattr(cli, "retrieval_stores", lambda *args: (meta, docs))
    assert cli.main(["--grid", "experiments/t017.yaml"]) == int(missing_queries)
    paths = list(Path(settings["evaluation"]["results_dir"]).glob("*.json"))
    assert len(paths) == 23
    report = json.loads(next(path for path in paths if path.stem.endswith("-grid")).read_text(encoding="utf-8"))
    assert report["summary"]["combinations"] == 22 and report["llm_calls"] == 0
    assert all(row["summary"]["returned_counts"] == {"1": 2} for row in report["rows"] if not row.get("blocked_reason"))
    assert report["summary"]["blocked"] == (["e3-02", "e3-03"] if missing_queries else [])
    for line in grid_markdown(report["rows"], report["summary"]["selections"]).splitlines():
        if line.startswith("|"):
            assert line.count("|") == 27
    assert "판정" in next(path for path in paths if path.stem.endswith("-grid")).with_suffix(".md").read_text(encoding="utf-8")
    compare = script("compare_mmr")
    monkeypatch.setattr(compare, "configured", lambda args: settings)
    monkeypatch.setattr(compare, "temporary_snapshot", lambda *args: nullcontext((object(), {"name": "fake"})))
    monkeypatch.setattr(compare, "retrieval_stores", lambda *args: (meta, docs))
    assert compare.main(["--ids", "R-2,R-01"]) == 0
    report = json.loads(next(Path(settings["evaluation"]["results_dir"]).glob("*compare-mmr.json")).read_text(encoding="utf-8"))
    assert [row["id"] for row in report["rows"]] == ["R-2", "R-01"]
    assert compare.main(["--ids", "bad"]) == 1


def test_missing_key_for_explicit_query_embedding(settings, tmp_path):
    from rag_service.llm import MissingApiKey

    settings["paths"]["embed_cache"] = str(tmp_path / "absent.sqlite")
    reader = QueryEmbeddings(settings, embed_missing=True)
    with pytest.raises(MissingApiKey):
        reader.embed_query("new question")
    assert not Path(settings["paths"]["embed_cache"]).exists()
