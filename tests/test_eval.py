"""평가 순수 함수·고정 입력·CLI를 네트워크 없이 검증한다."""

import importlib.util
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from test_index_items_offline import offline
from retrieval_helpers import configure_similarity
from test_collect_offline import FakeModel
from rag_service.config import load_settings, project_root
from rag_service.eval.common import digest, overrides, safe_name, write_result
from rag_service.eval.draft import generate_drafts, overlap_ratio, sample_sections, sections
from rag_service.eval.latency import markdown as latency_markdown, measure, percentile, representatives, summarize_latency
from rag_service.eval.metrics import label_hit, mrr, recall_at_k, summarize
from rag_service.eval.retrieval import compare_results, evaluate, frozen_terms
from rag_service.eval.routing import evaluate as evaluate_routing
from rag_service.eval.schema import read_cases, validate_rows
from rag_service.eval.snapshot import manifest_for


@pytest.mark.parametrize("value,display", [(1234.567891234567, "1235"), (0.0, "0"), (None, "None")])
def test_latency_markdown_rounds_only_display_values(value, display):
    group = {"count": 0 if value is None else 1, "failures": 0,
             "median_ms": value, "p90_ms": value,
             "timings": {"retrieval_ms": {"count": 1, "median_ms": value, "p90_ms": value}},
             "collection": dict.fromkeys(("calls", "searched", "skipped_queries", "failures", "partial"), 0)}
    rendered = latency_markdown({"warm:fact": group})
    timing_rows = [line for line in rendered.splitlines() if line.startswith("| warm:fact |")][:2]
    assert len(timing_rows) == 2
    assert all(line.endswith(f"| {display} | {display} |") for line in timing_rows)
    assert group["median_ms"] == value and group["timings"]["retrieval_ms"]["median_ms"] == value


@pytest.fixture
def settings(tmp_path):
    settings = load_settings()
    configure_similarity(settings)
    settings["llm"]["max_retries"] = 0
    settings["evaluation"]["results_dir"] = str(tmp_path / "results")
    return settings


def case(**changes):
    return {"id": "R-01", "set_num": "12345-1", "lang": "ko", "question": "역할은?",
            "relevant": [{"doc_id": "en:a", "section_path": "History"}],
            "kind": "entity", "source": "pm", **changes}


def routing_case(**changes):
    return {"id": "RT-01", "question": "부품 수?", "selected_set": None, "history": [],
            "has_collection": False, "expected_route": "fact", **changes}


def test_prefix_and_document_matching():
    label = [{"doc_id": "en:a", "section_path": "History"}]
    assert label_hit({"doc_id": "en:a", "section_path": "History › Film"}, label)
    assert not label_hit({"doc_id": "ko:a", "section_path": "History"}, label)
    assert not label_hit({"doc_id": "en:a", "section_path": "Other"}, label)
    assert label_hit({"doc_id": "en:a", "section_path": "Other"}, label, "doc")
    assert label_hit({"doc_id": "en:a"}, [{"doc_id": "en:a", "section_path": ""}])


def test_recall_unique_labels_and_first_reciprocal_rank():
    relevant = [{"doc_id": "a", "section_path": "H"}, {"doc_id": "a", "section_path": "D"},
                {"doc_id": "a", "section_path": "D"}, {"doc_id": "b", "section_path": "H"}]
    ranked = [{"doc_id": "x", "section_path": ""}, {"doc_id": "a", "section_path": "H child"},
              {"doc_id": "a", "section_path": "H child"}]
    assert recall_at_k(ranked, relevant, 1) == 0
    assert recall_at_k(ranked, relevant, 3) == pytest.approx(1 / 3)
    assert recall_at_k(ranked, relevant, 5, "doc") == .5
    assert mrr(ranked, relevant) == .5
    assert mrr([], relevant) == recall_at_k([], relevant, 5) == 0
    assert recall_at_k(ranked, [], 5) == recall_at_k(ranked, relevant, 0) == 0
    with pytest.raises(ValueError):
        recall_at_k(ranked, relevant, -1)
    with pytest.raises(ValueError):
        mrr([], [], "invalid")


def test_averages_are_macro_and_split_by_each_dimension():
    rows = [{"lang": "ko", "kind": "entity", "metrics": {"score": .5}},
            {"lang": "en", "kind": "series", "metrics": {"score": 1.0}}]
    result = summarize(rows, ["lang", "kind"])
    assert result["overall"] == {"count": 2, "metrics": {"score": .75}}
    assert result["lang"]["ko"]["metrics"]["score"] == .5
    assert result["kind"]["series"]["count"] == 1


@pytest.mark.parametrize("change", [{"relevant": []}, {"lang": "jp"}, {"source": "unknown"},
                                    {"id": ""}, {"question": " "}, {"set_num": 123},
                                    {"relevant": [{"doc_id": "a"}]}])
def test_retrieval_schema_rejects_bad_rows(change):
    with pytest.raises(ValueError):
        validate_rows([case(**change)], "retrieval")


def test_schemas_legacy_duplicate_empty_and_compare(tmp_path):
    legacy = read_cases(project_root() / "eval/route_questions.jsonl", "routing")
    assert len(legacy) == 24 and all(row["tags"] == [] for row in legacy)
    assert any(row.get("collection_sets") for row in legacy)
    with pytest.raises(ValueError):
        validate_rows([case(), case()], "retrieval")
    with pytest.raises(ValueError):
        validate_rows([], "retrieval")
    with pytest.raises(ValueError):
        validate_rows([routing_case(has_collection="false")], "routing")
    compare = {"id": "B-1", "question": "질문", "set_num": "A-1", "type": "fact",
               "facts": [{"field": "num_parts", "value": 42}]}
    assert validate_rows([compare], "rag_compare")[0]["facts"][0]["value"] == 42
    path = tmp_path / "bad.jsonl"
    path.write_text("not json", encoding="utf-8")
    with pytest.raises(ValueError):
        read_cases(path, "routing")


def test_settings_overrides_do_not_mutate(settings):
    updated = overrides(settings, ["retrieval.top_k=8", "llm.cache_enabled=false"])
    assert updated["retrieval"]["top_k"] == 8 and not updated["llm"]["cache_enabled"]
    assert settings["retrieval"]["top_k"] == 5 and settings["llm"]["cache_enabled"]


@pytest.mark.parametrize("value", ["unknown.x=1", "retrieval.top_k=x", "retrieval.top_k=true",
                                   "retrieval.top_k=-1", "retrieval.search_type=unknown", "not-a-pair"])
def test_invalid_override(settings, value):
    with pytest.raises(ValueError):
        overrides(settings, [value])


@pytest.mark.parametrize("name", ["../outside", "..", "C:/other", "a/b", "a\\b", ""])
def test_names_cannot_escape_root(name):
    with pytest.raises(ValueError):
        safe_name(name)


def test_manifest_hashes_counts_and_content_change(settings):
    rows = [{"id": "a", "text": "A", "metadata": {"doc_id": "doc", "chunk_config_hash": "h"}, "embedding": [1.]},
            {"id": "b", "text": "B", "metadata": {"doc_id": "doc", "chunk_config_hash": "h"}, "embedding": [2.]}]
    first = manifest_for({"docs": rows}, settings, "test")
    other = manifest_for({"docs": list(reversed(rows))}, settings, "test")
    assert first["collections"] == other["collections"]
    info = first["collections"]["docs"]
    assert (info["chunk_count"], info["document_count"], info["chunk_config_hash"]) == (2, 1, ["h"])
    assert first["created_at"] and first["settings_hash"] == digest(settings)
    rows[0]["embedding"] = [3.]
    assert manifest_for({"docs": rows}, settings, "test")["collections"] != first["collections"]


def test_fixed_p1_cache_and_context_change(settings, tmp_path):
    path = tmp_path / "terms.jsonl"
    model = FakeModel([{"queries": [{"q": "Fighter", "lang": "en", "purpose": "role"}], "en_terms": ["Fighter"]}])
    first = frozen_terms(case(), "Title", path=path, settings=settings, model=model)
    assert first == frozen_terms(case(), "Title", path=path, settings=settings, model=Mock()) == ["Fighter"]
    assert len(model.messages) == 1 and len(path.read_text().splitlines()) == 1
    with pytest.raises(ValueError):
        frozen_terms(case(set_num="other"), "Title", path=path, settings=settings, model=Mock())
    frozen_terms(case(question="different"), "Title", path=path, settings=settings,
                 model=FakeModel([{"queries": [], "en_terms": ["ignored"]}]))
    assert len(path.read_text().splitlines()) == 2


def test_failed_p1_is_not_cached(settings, tmp_path):
    path = tmp_path / "terms.jsonl"
    with pytest.raises(ValueError):
        frozen_terms(case(), "Title", path=path, settings=settings, model=FakeModel([RuntimeError("secret")]))
    assert not path.exists()


def test_retrieval_repeatability_and_baseline(settings):
    cases = [case(), case(id="R-2", lang="en", kind="series")]
    rank = lambda row: [({"doc_id": "en:a", "section_path": "History child"}, .1)]
    rows, summary = evaluate(cases, rank, settings)
    assert (rows, summary) == evaluate(cases, rank, settings)
    assert summary["hit_questions"] == 2 and summary["overall"]["metrics"]["recall@5"] == 1
    baseline = {"rows": rows, "summary": summary, "snapshot": {"name": "x"}}
    compared = compare_results(rows, baseline, {"name": "x"}, settings)
    assert set(compared.values()) == {0}
    missed, _ = evaluate(cases, lambda row: [], settings)
    compared = compare_results(missed, baseline, {"name": "x"}, settings)
    assert compared["hit_question_delta"] == -2 and compared["primary_delta_pp"] == -100
    with pytest.raises(ValueError):
        compare_results(rows, baseline, {"name": "different"}, settings)


def test_stratified_sampling_and_unavailable_set_kind():
    pool = [{"doc_id": f"{lang}:{kind}:{i}", "section_path": "History", "kind": kind, "lang": lang, "sets": ["A", "B"]}
            for lang in ("ko", "en") for kind in ("entity", "series", "set") for i in range(25)]
    selected, info = sample_sections(pool, 60, .75, 16)
    assert selected == sample_sections(list(reversed(pool)), 60, .75, 16)[0]
    assert sum(row["lang"] == "ko" for row in selected) == 45
    assert all(sum(row["lang"] == lang and row["kind"] == kind for row in selected) == count
               for lang, count in (("ko", 15), ("en", 5)) for kind in ("entity", "series", "set"))
    pool = [row for row in pool if row["kind"] != "set"]
    selected, info = sample_sections(pool, 60, .75, 16)
    assert len(selected) == 60 and info["unavailable_kinds"] == {"ko": ["set"], "en": ["set"]}
    with pytest.raises(ValueError):
        sample_sections([], 60, .75, 16)


def test_sections_group_chunks_and_restrict_to_snapshot_sets():
    meta = {"doc_id": "en:a", "section_path": "History", "scope": "entity", "lang": "en", "sets": ["A", "outside"]}
    records = [{"id": "2", "text": "second", "metadata": {**meta, "seq": 2}},
               {"id": "1", "text": "first", "metadata": {**meta, "seq": 1}}]
    result = sections(records, ["A"])
    assert len(result) == 1 and result[0]["text"] == "first\nsecond" and result[0]["sets"] == ["A"]
    assert not sections(records, ["B"])
    records[0]["metadata"]["scope"] = "series"
    assert sections(records[:1], ["B"])[0]["sets"] == ["B"]


def test_draft_prompt_and_overlap(settings):
    selected = [{"lang": "ko", "kind": "entity", "set_num": "A", "doc_id": "en:a", "section_path": "H",
                 "title": "title", "url": "url", "license": "license", "text": "abc def ghi", "chunk_ids": ["1"]}]
    model = FakeModel([{"question": "abc def?"}])
    rows = generate_drafts(selected, model=model, settings=settings, snapshot={"name": "test"})
    assert rows[0]["section_text"] == selected[0]["text"] and rows[0]["source"] == "draft-llm"
    assert rows[0]["longest_common_substring_ratio"] == pytest.approx(7 / 8)
    assert "그대로 옮기지 말고" in model.messages[0][0].content
    assert overlap_ratio("", "text") == 0 and overlap_ratio("ABC", "abc") == 1


def test_routing_accuracy_errors_and_confusion():
    cases = [routing_case(), routing_case(id="RT-2", expected_route="build"), routing_case(id="RT-3")]
    values = iter([{"predicted_route": "fact"}, {"predicted_route": "fact"}, {"predicted_route": None}])
    rows, result = evaluate_routing(cases, lambda _: next(values))
    assert result["accuracy"] == pytest.approx(1 / 3)
    assert result["confusion"]["build"]["fact"] == result["confusion"]["fact"]["error"] == 1
    assert result["incorrect_ids"] == ["RT-2", "RT-3"]


def test_latency_warmup_failures_and_stage_counts():
    cases = [routing_case()]
    responses = [SimpleNamespace(route="fact", answer="ok", needs_input="", warnings=[], usage={}, timings={"routing_ms": i})
                 for i in (1, 2, 3, 4)]
    responses[-1].needs_input = "failed"
    run = Mock(side_effect=responses)
    rows = measure(cases, run, reps=3, condition="warm", cache_enabled=True)
    assert run.call_count == 4 and [row["timings"]["routing_ms"] for row in rows] == [2, 3, 4]
    summary = summarize_latency(rows)["warm:fact"]
    assert summary["count"] == 2 and summary["failures"] == 1
    assert summary["timings"]["routing_ms"]["median_ms"] == 2.5
    assert percentile([1, 2, 3], .9) == 2.8
    assert representatives(cases * 2) == cases
    with pytest.raises(ValueError):
        representatives([routing_case(has_collection=True)])


def test_result_json_markdown_and_no_overwrite(settings):
    path = write_result("test", "test", settings, [{"id": "a"}], {"count": 1}, "표", snapshot={"name": "fixed"})
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["snapshot"] == {"name": "fixed"} and saved["rows"] == [{"id": "a"}]
    assert "표" in path.with_suffix(".md").read_text(encoding="utf-8")
    with pytest.raises(ValueError):
        write_result("test", "test", settings, [], {}, "")


def script(name):
    spec = importlib.util.spec_from_file_location(name, project_root() / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("name", ["eval_draft_retrieval", "eval_routing", "eval_latency"])
def test_cli_missing_key_one_line(name, capsys):
    assert script(name).main([]) == 1
    stderr = capsys.readouterr().err
    assert len(stderr.splitlines()) == 1 and "GMS_KEY" in stderr and "Traceback" not in stderr


def test_legacy_cli_validation_without_key(capsys):
    assert script("eval_routing").main(["--file", "eval/route_questions.jsonl", "--validate-only"]) == 0
    assert "24" in capsys.readouterr().out


def test_real_snapshot_clone_independence_and_corruption(settings, tmp_path):
    from rag_service.eval.snapshot import create_snapshot, load_snapshot
    from rag_service.vectorstore.store import get_collection
    from test_index_items_offline import cached

    settings["paths"]["chroma_dir"] = str(tmp_path / "source")
    settings["evaluation"]["snapshot_dir"] = str(tmp_path / "snapshots")
    embeddings = cached(tmp_path)
    meta = get_collection(settings["collections"]["items"], embeddings, settings)
    docs = get_collection(settings["collections"]["docs"], embeddings, settings)
    meta.add_texts(["meta"], ids=["A"], metadatas=[{"name": "A"}])
    docs.add_texts(["text"], ids=["a"], metadatas=[{"doc_id": "doc", "chunk_config_hash": "hash"}])
    manifest = create_snapshot(settings, "fixed")
    assert manifest["collections"][settings["collections"]["docs"]]["chunk_count"] == 1
    docs.add_texts(["later"], ids=["b"], metadatas=[{"doc_id": "second"}])
    client, saved = load_snapshot(settings, "fixed")
    assert saved == manifest
    snap_docs = client.get_collection(settings["collections"]["docs"])
    assert snap_docs.count() == 1 and docs._collection.count() == 2
    snap_docs.update(ids=["a"], metadatas=[{"doc_id": "changed"}])
    with pytest.raises(ValueError):
        load_snapshot(settings, "fixed")
    with pytest.raises(ValueError):
        create_snapshot(settings, "fixed")


def test_rank_docs_matches_service_with_injection_and_backfill(settings):
    from rag_service.rag.evidence import Card
    from rag_service.rag.graph import RagDeps
    from rag_service.rag.retrieve import _meta_card, rank_docs, retrieve
    from test_retrieve_offline import hit

    meta = Mock()
    meta.get.return_value = {"ids": ["A"], "metadatas": [{"name": "Title"}], "documents": ["meta"]}
    docs = Mock()
    docs.similarity_search_with_score.return_value = [hit("duplicate", text="injected"),
        hit("meta", text="meta"), *[hit(str(i), doc_id="same") for i in range(4)],
        hit("other"), hit("last")]
    injected = Card(0, "db", "injection", "url", "license", "injected", chunk_id="db")
    ranked = rank_docs("question", "A", docs_store=docs, settings=settings,
                       meta=_meta_card(meta, "A"), injected_cards=[injected])
    response = retrieve({"set_num": "A", "question": "question", "injected_cards": [injected]},
                        RagDeps(settings=settings, meta_store=meta, docs_store=docs))
    assert [row["chunk_id"] for row, _ in ranked] == [card.chunk_id for card in response["cards"][2:]]
    assert [row["chunk_id"] for row, _ in ranked] == ["0", "1", "other", "last"]
    assert all("page_content" not in row for row, _ in ranked)


def test_cold_router_and_search_bypass_reads_and_writes(settings, tmp_path):
    import sqlite3
    from rag_service.rag.schemas import RouterOutputV2, FilterQuery
    from rag_service.services.orchestrator import OrchestratorDeps, OrchestratorResponse, _cached
    from rag_service.services.search import _cached_call

    settings["llm"]["cache_enabled"] = False
    cache = Mock()
    model = FakeModel([{"route": "fact", "confidence": 1}, {"route": "fact", "confidence": 1}, {}])
    conn = sqlite3.connect(":memory:")
    try:
        deps = OrchestratorDeps(conn, settings, model=model, cache=cache)
        for _ in range(2):
            assert _cached(deps, [], RouterOutputV2, "v", OrchestratorResponse(), "router").route == "fact"
        assert _cached_call(model, [], FilterQuery, "v", cache, settings, {}, "filter") is not None
        cache.get_llm_cache.assert_not_called()
        cache.save_llm_cache.assert_not_called()
        assert len(model.messages) == 3
    finally:
        conn.close()


def test_cold_p1_bypasses_cache_without_collection_calls(settings):
    from rag_service.rag.collect import _Collection

    settings["llm"]["cache_enabled"] = False
    cache, collector = Mock(), Mock()
    model = FakeModel([{"queries": [{"q": "ship", "lang": "en", "purpose": "role"}], "en_terms": ["ship"]}])
    collection = _Collection("A", "role", settings, None, model, collector, cache, None, None, None)
    collection.extract("Ship")
    assert collection.result.en_terms == ["ship"]
    cache.get_llm_cache.assert_not_called()
    cache.save_llm_cache.assert_not_called()
    collector.search.assert_not_called()


def test_cold_p1_keeps_completed_search_manifest(settings, tmp_path):
    from rag_service.data_prep.doc_store import DocStore
    from rag_service.rag.collect import _Collection

    store = DocStore(tmp_path / "docs", tmp_path / "collect.sqlite")
    store.log_fetch("wikipedia-search", "ship", None, None, "fetched")
    settings["llm"]["cache_enabled"] = False
    cache = Mock(wraps=store)
    collector, docs, embeddings = Mock(), Mock(), Mock()
    docs.get.return_value = {"metadatas": []}
    model = FakeModel([{"queries": [{"q": "ship", "lang": "en", "purpose": "role"}],
                        "en_terms": ["ship"]}] * 2)
    for _ in range(2):
        flow = _Collection("A", "role", settings, None, model, collector, cache, docs, embeddings, None)
        flow.extract("Ship")
        assert flow.search() == {}
        assert flow.result.searched == [] and flow.result.skipped_queries == ["ship"]
    assert len(model.messages) == 2 and store.queried("wikipedia-search", "ship")
    cache.get_llm_cache.assert_not_called()
    cache.save_llm_cache.assert_not_called()
    cache.log_fetch.assert_not_called()
    collector.search.assert_not_called()
    collector.fetch.assert_not_called()
    embeddings.embed_documents.assert_not_called()


def test_retrieval_cli_twice_on_real_fixture_snapshot(settings, tmp_path, monkeypatch):
    from langchain_chroma import Chroma
    from rag_service.eval.common import write_jsonl
    from rag_service.eval.snapshot import create_snapshot
    from rag_service.vectorstore.store import get_collection
    from test_index_items_offline import cached

    settings["paths"]["chroma_dir"] = str(tmp_path / "source")
    settings["evaluation"].update(snapshot_dir=str(tmp_path / "snapshots"), snapshot_name="fixed",
                                   retrieval_file=str(tmp_path / "questions.jsonl"),
                                   en_terms_cache=str(tmp_path / "terms.jsonl"))
    embeddings = cached(tmp_path)
    meta = get_collection(settings["collections"]["items"], embeddings, settings)
    docs = get_collection(settings["collections"]["docs"], embeddings, settings)
    meta.add_texts(["set metadata"], ids=["12345-1"], metadatas=[{"name": "Title"}])
    docs.add_texts(["History passage"], ids=["entity:en:a:0"], metadatas=[{
        "source_type": "wikipedia", "doc_id": "en:a", "title": "Title", "url": "url", "license": "CC",
        "lang": "en", "scope": "entity", "sets": ["12345-1"], "section_path": "History child"}])
    create_snapshot(settings, "fixed")
    write_jsonl(Path(settings["evaluation"]["retrieval_file"]), [case()])
    cli = script("eval_retrieval")
    monkeypatch.setattr(cli, "configured", lambda args: settings)
    model = FakeModel([{"queries": [{"q": "History", "lang": "en", "purpose": "role"}], "en_terms": ["History"]}])
    frozen_terms(case(), "Title", path=Path(settings["evaluation"]["en_terms_cache"]),
                 settings=settings, model=model)
    def snapshot_stores(client, config, embed_missing):
        return tuple(Chroma(client=client, collection_name=config["collections"][key], embedding_function=embeddings,
                            create_collection_if_not_exists=False) for key in ("items", "docs"))
    monkeypatch.setattr(cli, "retrieval_stores", snapshot_stores)
    assert cli.main(["--name", "one"]) == 0
    first_path = next(Path(settings["evaluation"]["results_dir"]).glob("*_one.json"))
    assert cli.main(["--name", "two", "--baseline", str(first_path)]) == 0
    second_path = next(Path(settings["evaluation"]["results_dir"]).glob("*_two.json"))
    first, second = (json.loads(path.read_text(encoding="utf-8")) for path in (first_path, second_path))
    assert [row["ranked"] for row in first["rows"]] == [row["ranked"] for row in second["rows"]]
    assert first["summary"]["overall"] == second["summary"]["overall"]
    assert first["summary"]["overall"]["metrics"]["recall@5"] == 1
    assert second["comparison"]["hit_question_delta"] == 0
    assert first["collect_fn"] is None and len(model.messages) == 1


def test_generate_sixty_drafts_with_fake_llm(settings):
    pool = [{"doc_id": f"{lang}:{kind}:{i}", "section_path": "History", "kind": kind, "lang": lang,
             "sets": ["12345-1"], "title": "Title", "text": "A passage for a question.", "url": "url",
             "license": "CC", "chunk_ids": [str(i)]}
            for lang in ("ko", "en") for kind in ("entity", "series", "set") for i in range(25)]
    selected, _ = sample_sections(pool, 60, .75, 16)
    model = FakeModel([{"question": f"사용자 질문 {i}?"} for i in range(60)])
    rows = generate_drafts(selected, model=model, settings=settings, snapshot={"name": "fake"})
    assert len(validate_rows(rows, "retrieval")) == len(model.messages) == 60
    assert sum(row["lang"] == "ko" for row in rows) == 45


def test_routing_cli_output_legacy_with_fake_predictions(settings, monkeypatch):
    from contextlib import contextmanager

    cli = script("eval_routing")
    monkeypatch.setattr(cli, "configured", lambda args: settings)
    @contextmanager
    def fake_deps(config):
        yield object()
    monkeypatch.setattr(cli, "dependencies", fake_deps)
    monkeypatch.setattr(cli, "predict", lambda row, deps: {"predicted_route": row["expected_route"]})
    assert cli.main(["--file", "eval/route_questions.jsonl", "--name", "fake-route"]) == 0
    report = json.loads(next(Path(settings["evaluation"]["results_dir"]).glob("*.json")).read_text(encoding="utf-8"))
    assert report["summary"]["count"] == 24 and report["summary"]["accuracy"] == 1


@pytest.mark.parametrize("snapshot", [False, True])
def test_latency_cli_three_repetitions_both_conditions(settings, monkeypatch, snapshot):
    from contextlib import contextmanager

    cli = script("eval_latency")
    monkeypatch.setattr(cli, "configured", lambda args: settings)
    monkeypatch.setattr(cli, "get_chat_model", lambda config: object())
    snapshot_loader = Mock(return_value=(object(), {"name": "fake"}))
    monkeypatch.setattr(cli, "load_snapshot", snapshot_loader)
    seen = []
    paths = []
    callbacks = []
    @contextmanager
    def fake_deps(config, client, live, on_collect):
        from rag_service.services.orchestrator import OrchestratorDeps
        assert live is not snapshot and (client is None) is not snapshot
        for key in ("collect_sqlite", "docs_dir", "embed_cache", "chroma_dir"):
            assert config["paths"][key] == settings["paths"][key]
        paths.append(Path(config["paths"]["user_sqlite"]))
        assert paths[-1] != Path(settings["paths"]["user_sqlite"])
        callbacks.append(on_collect)
        yield OrchestratorDeps(conn=None, settings=config)
    monkeypatch.setattr(cli, "dependencies", fake_deps)
    rows = read_cases(project_root() / "eval/route_questions.jsonl", "routing")
    routes = {row["question"]: row["expected_route"] for row in rows}
    def fake_answer(question, *, selected_set, history, collection_id, deps):
        seen.append(deps.settings["llm"]["cache_enabled"])
        if routes[question] in {"build", "build_archive"}:
            assert collection_id and deps.collection_store.get(collection_id)["sets"]
        if not snapshot:
            callbacks[-1](SimpleNamespace(searched=["new"], skipped_queries=["old", "old2"], partial=False))
        return SimpleNamespace(route=routes[question], answer="ok", needs_input="", warnings=[],
                               timings={"routing_ms": 1, "total_ms": 2}, usage={})
    monkeypatch.setattr(cli, "answer", fake_answer)
    assert cli.main(["--reps", "3", "--name", "fake-latency", "--file", "eval/route_questions.jsonl",
                     *(["--snapshot", "fake"] if snapshot else [])]) == 0
    report = json.loads(next(Path(settings["evaluation"]["results_dir"]).glob("*.json")).read_text(encoding="utf-8"))
    assert len(report["rows"]) == 42 and len(report["summary"]) == 14
    assert seen.count(True) == 28 and seen.count(False) == 21
    assert all(group["count"] == 3 and group["failures"] == 0 for group in report["summary"].values())
    assert snapshot_loader.call_count == int(snapshot)
    assert report["mode"] == ("snapshot" if snapshot else "live")
    assert report["user_db"] == "temporary" and all(not path.exists() for path in paths)
    assert all(row["collection"]["searched"] == (0 if snapshot else 1) for row in report["rows"])
    assert all(row["collection"]["skipped_queries"] == (0 if snapshot else 2) for row in report["rows"])
    assert all(group["collection"]["calls"] == (0 if snapshot else 3) for group in report["summary"].values())


def test_latency_representatives_fallback_and_temporary_db(settings):
    from rag_service.eval.runtime import temporary_collections

    original = [routing_case(expected_route="build", has_collection=True),
                routing_case(id="RT-2", expected_route="build", collection_sets=[["99999-1", 2]]),
                routing_case(id="RT-3", expected_route="build_archive", collection_sets=[["12345-1", 3]])]
    before = deepcopy(original)
    cases = representatives(original, settings["evaluation"]["latency_collection"])
    assert [row["id"] for row in cases] == ["RT-01", "RT-3"]
    assert cases[0]["collection_sets"] == settings["evaluation"]["latency_collection"]
    with temporary_collections(cases) as (store, ids):
        path = store.path
        assert sorted(store.get(ids["RT-01"])["sets"]) == sorted(map(tuple, cases[0]["collection_sets"]))
        assert store.get(ids["RT-3"])["sets"] == [("12345-1", 3)]
    assert not path.exists() and original == before


@pytest.mark.parametrize("cache_enabled", [True, False])
def test_live_dependencies_collect_and_cache_settings(settings, monkeypatch, tmp_path, cache_enabled):
    import sqlite3
    from rag_service.eval import runtime
    from rag_service.rag import collect, graph
    from rag_service.data_prep.collectors import http, wikipedia
    from rag_service.data_prep import doc_store
    from rag_service.vectorstore import embed_cache, store

    settings["paths"]["sqlite"] = str(tmp_path / "lego.sqlite")
    sqlite3.connect(settings["paths"]["sqlite"]).close()
    settings["llm"]["cache_enabled"] = cache_enabled
    model, inner, embeddings, meta, docs, document_cache = [object() for _ in range(6)]
    monkeypatch.setattr(runtime, "get_chat_model", lambda config: model)
    monkeypatch.setattr(runtime, "get_embeddings", lambda config, **kw: inner)
    http_client = SimpleNamespace(session=Mock())
    monkeypatch.setattr(http, "HttpClient", Mock(return_value=http_client))
    monkeypatch.setattr(http, "build_user_agent", lambda *args: "Test/1")
    wiki = object()
    monkeypatch.setattr(wikipedia, "WikipediaCollector", Mock(return_value=wiki))
    doc_factory = Mock(return_value=document_cache)
    embed_factory = Mock(return_value=embeddings)
    meta_factory, docs_factory = Mock(return_value=meta), Mock(return_value=docs)
    monkeypatch.setattr(doc_store, "DocStore", doc_factory)
    monkeypatch.setattr(embed_cache, "CachedEmbeddings", embed_factory)
    monkeypatch.setattr(store, "get_collection", meta_factory)
    monkeypatch.setattr(store, "get_docs_collection", docs_factory)
    result = SimpleNamespace(searched=["new"], skipped_queries=["cached"], partial=False)
    collector = Mock(return_value=result)
    monkeypatch.setattr(collect, "collect_for_question", collector)
    seen = []

    def fake_ask(*args, deps, **kwargs):
        assert deps.settings is settings and deps.model is model
        assert deps.settings["llm"]["cache_enabled"] is cache_enabled
        assert deps.meta_store is meta and deps.docs_store is docs
        assert callable(deps.collect_fn)
        assert deps.collect_fn("12345-1", "question") is result
        return {"answer": "ok"}

    monkeypatch.setattr(graph, "ask", fake_ask)
    with runtime.dependencies(settings, live=True, on_collect=seen.append) as deps:
        assert not meta_factory.called  # 템플릿 경로에서는 색인을 열지 않는다.
        assert deps.ask_fn("12345-1", "question", deps=None) == {"answer": "ok"}
        kwargs = collector.call_args.kwargs
        assert kwargs["settings"] is settings and kwargs["conn"] is deps.conn
        assert kwargs["collector"] is wiki and kwargs["doc_store"] is document_cache
        assert kwargs["embeddings"] is embeddings
    assert seen == [result]
    doc_factory.assert_called_once_with(runtime.resolve_path(settings["paths"]["docs_dir"]),
                                        runtime.resolve_path(settings["paths"]["collect_sqlite"]))
    assert embed_factory.call_args.args[1] == runtime.resolve_path(settings["paths"]["embed_cache"])
    assert meta_factory.call_args.args[-1] is docs_factory.call_args.args[-1] is settings
    http_client.session.close.assert_called_once()
