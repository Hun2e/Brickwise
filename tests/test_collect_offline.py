"""가짜 모델·수집기와 실제 임시 Chroma로 질문 수집을 검증한다."""

import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import replace
from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage
from pydantic import ValidationError

from test_index_items_offline import cached, load_script, offline, settings
from rag_service.config import project_root
from rag_service.data_prep.collectors.base import Candidate, Doc, Section
from rag_service.data_prep.doc_store import DocStore
from rag_service.llm import MissingApiKey
from rag_service.rag import collect
from rag_service.rag.prompts import p1_messages, p2_messages
from rag_service.rag.schemas import CandidateSelection, QueryExtraction, QueryExtractionV2, SearchQueryV2, p1_schema
from rag_service.vectorstore.doc_index import DocWithScope, sync_set_docs
from rag_service.vectorstore.store import get_docs_collection


class FakeModel:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.messages = []

    def with_structured_output(self, schema, **kwargs):
        return self

    def invoke(self, messages):
        self.messages.append(messages)
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return {"parsed": response, "parsing_error": None,
                "raw": AIMessage(content="", usage_metadata={"input_tokens": 10, "output_tokens": 5,
                                                             "total_tokens": 15})}


def p1(*queries):
    return {"queries": [{"q": q, "lang": "en", "purpose": "기체 설명"} for q in queries],
            "en_terms": ["Republic attack gunship"]}


def p2(*ids):
    return {"selected": [{"id": ident, "reason": "질문 관련 기체"} for ident in ids]}


@pytest.fixture
def flow(tmp_path, settings):
    settings["llm"]["max_retries"] = 0
    settings["collection"]["question_include_korean"] = False
    settings["chunking"]["min_chunk_chars"] = 1
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE sets(set_num TEXT, name TEXT)")
    conn.executemany("INSERT INTO sets VALUES (?, ?)", [("75076-1", "Republic Gunship"), ("A", "Other")])
    conn.execute("CREATE TABLE set_entities(set_num TEXT, wiki_title TEXT, kind TEXT)")
    store = DocStore(tmp_path / "docs", tmp_path / "collect.sqlite")
    docs = [Doc(f"wp-en:{i}", "wikipedia", "en", i, title, f"https://en.wikipedia.org/wiki/{i}",
                1, "CC BY-SA 4.0", "Wikipedia contributors", [Section("", 1, "A fictional vehicle in Star Wars. " * 20)],
                "2026-10-21", None)
            for i, title in enumerate(["Republic attack gunship", "List of Star Wars vehicles"], 1)]
    collector = Mock(source="wikipedia")
    collector.search.return_value = [Candidate(d.source, d.lang, d.page_id, d.title, "Star Wars vehicle " * 40, d.url)
                                     for d in docs]
    collector.latest_revid.side_effect = lambda lang, title: next(
        (d.page_id, d.revid, d.title) for d in docs if (d.lang, d.title) == (lang, title))
    collector.fetch.side_effect = lambda lang, ident, search_query=None: replace(next(
        d for d in docs if (d.lang, d.page_id) == (lang, ident)), search_query=search_query)
    collector.langlinks.return_value = {docs[0].title: None, docs[1].title: None}
    embeddings = cached(tmp_path)
    events = []
    args = dict(settings=settings, conn=conn, model=FakeModel([p1("Republic attack gunship"), p2("wp-en:1")]),
                collector=collector, doc_store=store, docs_store=get_docs_collection(embeddings, settings),
                embeddings=embeddings, on_event=lambda name, data: events.append((name, data)))
    yield args, docs, events
    conn.close()


def run(flow, question="영화에서 어떤 역할?", **overrides):
    args, _, _ = flow
    return collect.collect_for_question("75076-1", question, **{**args, **overrides})


def test_prompt_versions_and_limits():
    v2 = p1_messages("Gunship", "75076-1", "question", [])
    assert all(token in v2[0].content for token in ("p1-v2", "세트 번호", "따옴표", "최대 3개", "첫 번째", "핵심 대상"))
    assert "p1-v1" in p1_messages("Gunship", "75076-1", "question", [], "p1-v1")[0].content
    assert "최대 4개" in p1_messages("Gunship", "75076-1", "question", [], "p1-v1")[0].content
    with pytest.raises(ValidationError):
        SearchQueryV2(q="a", lang="en", purpose="x" * 41)
    with pytest.raises(ValidationError):
        CandidateSelection(selected=[{"id": "a", "reason": "x" * 41}])
    message = p2_messages("set", "question", [{"id": "1", "title": "title", "snippet": "a" * 300, "lang": "en"}], 2)
    assert len(json.loads(message[1].content)["candidates"][0]["snippet"]) == 150


@pytest.mark.parametrize("version", ["p1-v1", "p1-v2", "p1-v2.1"])
def test_p1_versioned_schema_and_prompt_match(flow, version):
    args, _, _ = flow
    args["settings"]["collection"].update(p1_prompt_version=version, max_queries=4)
    payload = p1(*[f"Star Wars vehicle {i}" for i in range(4 if version == "p1-v1" else 3)])
    payload["queries"][0]["purpose"] = "x" * (100 if version == "p1-v1" else 40)
    args["model"] = FakeModel([payload, p2()])
    result = run(flow)
    assert not result.partial
    assert len(result.queries) == len(payload["queries"])
    assert result.queries[0].purpose == payload["queries"][0]["purpose"]
    schema = p1_schema(version)
    assert schema.model_validate(payload)
    assert json.dumps(schema.model_json_schema(), ensure_ascii=False) in args["model"].messages[0][0].content
    assert not run(flow).partial
    assert len(args["model"].messages) == 2


@pytest.mark.parametrize("count,purpose_length", [(4, 10), (1, 41)])
def test_v1_responses_are_rejected_by_v2(count, purpose_length):
    payload = p1(*[f"query {i}" for i in range(count)])
    payload["queries"][0]["purpose"] = "x" * purpose_length
    assert QueryExtraction.model_validate(payload)
    with pytest.raises(ValidationError):
        QueryExtractionV2.model_validate(payload)


def test_v21_does_not_reuse_v2_cache(flow):
    args, _, _ = flow
    args["settings"]["collection"]["p1_prompt_version"] = "p1-v2"
    args["model"] = FakeModel([p1("Ghost"), p2(), p1("Ghost Star Wars Rebels"), p2()])
    assert not run(flow).partial
    args["settings"]["collection"]["p1_prompt_version"] = "p1-v2.1"
    assert not run(flow).partial
    assert len(args["model"].messages) == 4
    prompt = args["model"].messages[2][0].content
    assert "한 단어 또는 일반 명사" in prompt and "Ghost Star Wars Rebels" in prompt
    assert "p1-v2.1" in prompt
    assert "실존 제품·인물·장소" in args["model"].messages[3][0].content


def test_domain_filter_precedes_p2_and_fallback(flow):
    args, _, _ = flow
    args["collector"].search.return_value.insert(0, Candidate(
        "wikipedia", "en", 90, "Rolls-Royce Ghost", "Luxury automobile", "url"))
    args["model"] = FakeModel([p1("Ghost Star Wars Rebels"), p2("wp-en:90", "wp-en:1")])
    result = run(flow)
    rows = json.loads(args["model"].messages[1][1].content)["candidates"]
    assert [row["id"] for row in rows] == ["wp-en:1", "wp-en:2"]
    assert result.fetched_doc_ids == ["wp-en:1"]
    assert ("wp-en:90", "Rolls-Royce Ghost", "도메인 단서 없음") in result.rejected


def test_domain_keywords_are_configurable_and_case_insensitive(flow):
    args, _, _ = flow
    args["settings"]["collection"]["domain_keywords"] = ["FICTIONAL VEHICLE"]
    args["collector"].search.return_value = [replace(
        args["collector"].search.return_value[0], snippet="A Fictional Vehicle")]
    assert run(flow).fetched_doc_ids == ["wp-en:1"]


@pytest.mark.parametrize("suffix", ["", "x" * 500 + " Star Wars"])
def test_p2_cannot_link_falcon9_from_misleading_snippet(flow, suffix):
    args, docs, _ = flow
    docs[0] = replace(docs[0], title="팰컨 9", sections=[Section(
        "", 1, "팰컨 9은 SpaceX가 개발한 재사용 가능한 2단 로켓 발사 시스템이다. " + suffix)])
    args["collector"].search.return_value = [Candidate(
        "wikipedia", "en", 1, "팰컨 9", "Star Wars Falcon", docs[0].url)]
    result = run(flow)
    assert not result.partial and not result.fetched_doc_ids
    assert ("wp-en:1", "팰컨 9", "도메인 단서 없음") in result.rejected
    assert not args["doc_store"].question_links()
    assert args["docs_store"].get()["ids"] == []
    calls = args["collector"].fetch.call_count
    assert not run(flow).partial
    assert args["collector"].fetch.call_count == calls


@pytest.mark.parametrize("title,intro", [("X-wing fighter", "A Star Wars vehicle."),
                                        ("레고 스타워즈", "제품 시리즈."),
                                        ("보바 펫", "은하계의 현상금 사냥꾼."),
                                        ("Republic attack gunship", "A fictional vehicle.")])
def test_domain_guard_accepts_title_or_intro(flow, title, intro):
    args, docs, _ = flow
    docs[0] = replace(docs[0], title=title, sections=[Section("", 1, intro)])
    args["collector"].search.return_value = [Candidate(
        "wikipedia", "en", 1, title, "Star Wars", docs[0].url)]
    assert run(flow).fetched_doc_ids == ["wp-en:1"]


@pytest.mark.parametrize("kind", ["video game", "novel", "comic", "board game", "card game"])
@pytest.mark.parametrize("location", ["title", "snippet"])
def test_media_candidates_excluded_before_p2(flow, kind, location):
    args, _, _ = flow
    candidate = Candidate("wikipedia", "en", 90, "Star Wars Battle for Naboo", "Star Wars", "url")
    candidate = replace(candidate, **{location: f"Star Wars {kind.upper()}"})
    args["collector"].search.return_value = [candidate]
    result = run(flow)
    assert not result.partial and result.candidates == 0
    assert len(args["model"].messages) == 1
    args["collector"].fetch.assert_not_called()


@pytest.mark.parametrize("question", ["게임", "소설", "코믹", "만화", "GAME", "Novel", "Comic"])
def test_media_question_preserves_kind_hint(flow, question):
    args, _, _ = flow
    args["collector"].search.return_value = [replace(
        args["collector"].search.return_value[0], snippet="Star Wars " + "x" * 150 + " video game")]
    result = run(flow, question=question)
    assert not result.partial and result.fetched_doc_ids == ["wp-en:1"]
    row = json.loads(args["model"].messages[1][1].content)["candidates"][0]
    assert row["kind_hint"] == "video game" and len(row["snippet"]) == 150


@pytest.mark.parametrize("failure", ["budget", "error", "missing"])
def test_incomplete_search_retries_without_refetching_saved_body(flow, monkeypatch, failure):
    args, docs, _ = flow
    now = [0.0]
    monkeypatch.setattr(collect.time, "monotonic", lambda: now[0])
    args["model"] = FakeModel([p1("Gunship"), p2("wp-en:1", "wp-en:2"), p2("wp-en:1", "wp-en:2")])
    fetch = args["collector"].fetch.side_effect
    def interrupted(lang, ident, search_query=None):
        if failure == "budget":
            now[0] = 31.0
        elif ident == 2:
            if failure == "error":
                raise RuntimeError("offline failure")
            return None
        return fetch(lang, ident, search_query)
    args["collector"].fetch.side_effect = interrupted
    first = run(flow)
    assert first.partial and first.fetched_doc_ids == ["wp-en:1"]
    assert not args["doc_store"].queried("wikipedia-search", "Gunship")
    args["collector"].fetch.reset_mock(side_effect=True)
    args["collector"].fetch.side_effect = fetch
    now[0] = 40.0
    second = run(flow)
    assert not second.partial and args["collector"].search.call_count == 2
    assert [call.args[1] for call in args["collector"].fetch.call_args_list] == [2]
    assert args["doc_store"].queried("wikipedia-search", "Gunship")
    assert {meta["doc_id"] for meta in args["docs_store"].get()["metadatas"]} == {"wp-en:1", "wp-en:2"}
    calls = len(args["collector"].mock_calls)
    assert not run(flow).partial
    assert len(args["collector"].mock_calls) == calls


def test_search_completion_tracks_all_queries_for_shared_candidate(flow):
    args, _, _ = flow
    args["model"] = FakeModel([p1("first", "shared", "independent"), p2("wp-en:1", "wp-en:2")])
    candidates = args["collector"].search.return_value
    args["collector"].search.side_effect = lambda q, *a: [candidates[0 if q == "independent" else 1]]
    fetch = args["collector"].fetch.side_effect
    args["collector"].fetch.side_effect = lambda lang, ident, search_query=None: (
        None if ident == 2 else fetch(lang, ident, search_query))
    assert run(flow).partial
    assert args["doc_store"].queried("wikipedia-search", "independent")
    assert not args["doc_store"].queried("wikipedia-search", "first")
    assert not args["doc_store"].queried("wikipedia-search", "shared")


@pytest.mark.parametrize("empty_search", [True, False])
def test_no_selected_documents_completes_query(flow, empty_search):
    args, _, _ = flow
    args["model"] = FakeModel([p1("Gunship"), p2()])
    if empty_search:
        args["collector"].search.return_value = []
    assert not run(flow).partial
    assert args["doc_store"].queried("wikipedia-search", "Gunship")
    calls = len(args["collector"].mock_calls)
    assert not run(flow).partial
    assert len(args["collector"].mock_calls) == calls


def test_normal_flow_and_second_call_has_no_external_calls(flow):
    args, _, events = flow
    result = run(flow)
    assert not result.partial and result.indexed_chunks > 0
    assert result.fetched_doc_ids == ["wp-en:1"]
    assert result.selected == [("wp-en:1", "질문 관련 기체")]
    assert [name for name, _ in events] == ["extracting_queries", "searching", "selecting", "selected", "fetching", "indexing", "done"]
    assert result.llm_usage["p1"] == {"calls": 1, "input_tokens": 10, "output_tokens": 5}
    calls, embedded = len(args["collector"].mock_calls), args["embeddings"].stats.copy()
    assert len(args["model"].messages) == 2
    events.clear()
    result = run(flow)
    assert not result.partial and result.searched == [] and result.indexed_chunks == 0
    assert result.skipped_queries == ["Republic attack gunship"]
    assert len(args["model"].messages) == 2 and len(args["collector"].mock_calls) == calls
    assert args["embeddings"].stats == embedded
    assert events[-1][0] == "skipped" and events[-1][1]["reason"] == "cached"
    assert result.llm_usage["p1"] == {"calls": 0, "cache_hit": True}


@pytest.mark.parametrize("cache_enabled", [True, False])
def test_cached_queries_emit_skipped_without_p1_cache(flow, cache_enabled):
    from rag_service.services.progress import StageTracker

    args, _, events = flow
    args["settings"]["llm"]["cache_enabled"] = cache_enabled
    assert not run(flow).partial
    calls = len(args["collector"].mock_calls)
    # 같은 질문도 P1 캐시를 우회하면 새 모델 호출이 발생한다.
    args["settings"]["llm"]["cache_enabled"] = False
    args["model"] = FakeModel([p1("Republic attack gunship")])
    events.clear()
    result = run(flow)
    assert not result.partial and result.llm_usage["p1"]["calls"] == 1
    assert not result.searched and not result.fetched_doc_ids and not result.indexed_chunks
    assert len(args["collector"].mock_calls) == calls
    assert events[-1][0] == "skipped" and events[-1][1]["reason"] == "cached"
    assert StageTracker().update(*events[-1]) == ("문서 수집", "이미 수집됨")


def test_failed_search_is_not_reported_as_cached(flow):
    args, _, events = flow
    args["collector"].search.side_effect = RuntimeError("offline failure")
    assert run(flow).partial
    assert events[-1][0] == "done" and "reason" not in events[-1][1]


def test_indexed_candidate_excluded_and_existing_documents_preserved(flow):
    args, docs, _ = flow
    args["doc_store"].save(docs[0])
    sync_set_docs(args["docs_store"], [DocWithScope(docs[0], "entity", docs[0].title, "vehicle")],
                  {docs[0].title: ["A"]}, args["settings"]["chunking"])
    args["conn"].execute("INSERT INTO set_entities VALUES (?, ?, ?)", ("A", docs[0].title, "vehicle"))
    args["model"] = FakeModel([p1("Republic attack gunship"), p2("wp-en:2")])
    result = run(flow)
    candidates = json.loads(args["model"].messages[1][1].content)["candidates"]
    assert [c["id"] for c in candidates] == ["wp-en:2"]
    assert result.fetched_doc_ids == ["wp-en:2"]
    metadata = args["docs_store"].get()["metadatas"]
    assert {m["doc_id"] for m in metadata} == {"wp-en:1", "wp-en:2"}
    assert {m["entity_kind"] for m in metadata} == {"vehicle", "list"}
    assert all("A" in m["sets"] for m in metadata if m["doc_id"] == "wp-en:1")


def test_empty_selection_is_normal(flow):
    args, _, _ = flow
    args["model"] = FakeModel([p1("Republic attack gunship"), p2()])
    result = run(flow)
    assert not result.partial and result.selected == [] and result.fetched_doc_ids == []
    args["collector"].fetch.assert_not_called()


def test_parsing_fallbacks_skip_game_and_series(flow):
    args, docs, _ = flow
    args["model"] = FakeModel([{"invalid": True}, {"invalid": True}])
    args["collector"].search.return_value[:0] = [
        Candidate("wikipedia", "en", 90, "Star Wars", "film series", "url"),
        Candidate("wikipedia", "en", 91, "Republic Commando", "video game", "url"),
        Candidate("wikipedia", "en", 92, "Novel", "novel", "url"),
    ]
    result = run(flow)
    assert result.partial and result.queries[0].q == "Republic Gunship"
    assert result.selected[0][0] == docs[0].doc_id and result.fetched_doc_ids == [docs[0].doc_id]


def test_p2_unknown_duplicate_and_limit(flow):
    args, _, _ = flow
    args["settings"]["collection"]["max_docs_per_question"] = 1
    args["model"] = FakeModel([p1("Republic attack gunship"), p2("unknown", "wp-en:2", "wp-en:2", "wp-en:1")])
    result = run(flow)
    assert result.selected == [("wp-en:2", "질문 관련 기체")]
    assert result.fetched_doc_ids == ["wp-en:2"]
    assert result.rejected == [("wp-en:1", "Republic attack gunship", "문서 수 상한 초과")]


def test_rejection_reasons_are_distinct_and_deduplicated(flow):
    from rag_service.services.progress import StageTracker

    args, _, events = flow
    args["settings"]["collection"]["max_docs_per_question"] = 1
    args["collector"].search.return_value.extend([
        Candidate("wikipedia", "en", 90, "Ghost", "A car", "url"),
        Candidate("wikipedia", "en", 91, "Jedi", "Star Wars character", "url"),
        Candidate("wikipedia", "en", 92, "Star Wars game", "video game", "url"),
    ])
    args["model"] = FakeModel([p1("first", "second"), p2("wp-en:1", "wp-en:2")])
    result = run(flow)
    assert result.selected_titles == {"wp-en:1": "Republic attack gunship"}
    assert {ident: reason for ident, _, reason in result.rejected} == {
        "wp-en:90": "도메인 단서 없음", "wp-en:91": "선별되지 않음",
        "wp-en:92": "질문에서 요청하지 않은 매체", "wp-en:2": "문서 수 상한 초과",
    }
    assert len(result.rejected) == 4
    event, = [event for event in events if event[0] == "selected"]
    assert event[1]["selected"] == ["Republic attack gunship"]
    assert StageTracker().update(*event) == ("문서 수집", "선택 1 · 제외 4")


def test_all_candidates_rejected_still_emits_summary(flow):
    args, _, events = flow
    args["collector"].search.return_value = [Candidate("wikipedia", "en", 90, "Ghost", "A car", "url")]
    result = run(flow)
    assert result.selected_titles == {} and len(result.rejected) == 1
    assert any(name == "selected" and data["selected"] == [] and data["rejected_count"] == 1
               for name, data in events)
    assert len(args["model"].messages) == 1


def test_budget_stops_after_search(flow, monkeypatch):
    args, _, events = flow
    now = [0.0]
    monkeypatch.setattr(collect.time, "monotonic", lambda: now[0])
    candidates = args["collector"].search.return_value
    def slow_search(*args):
        now[0] = 31.0
        return candidates
    args["collector"].search.side_effect = slow_search
    result = run(flow)
    assert result.partial and result.elapsed_ms == 31000
    assert len(args["model"].messages) == 1
    args["collector"].fetch.assert_not_called()
    assert events[-1][0] == "done"


def test_budget_stops_retry_and_does_not_search(flow, monkeypatch):
    args, _, _ = flow
    args["settings"]["llm"]["max_retries"] = 1
    now = [0.0]
    monkeypatch.setattr(collect.time, "monotonic", lambda: now[0])
    def slow_model(messages):
        now[0] = 31.0
        raise TimeoutError("private")
    args["model"].invoke = Mock(side_effect=slow_model)
    result = run(flow)
    assert result.partial and args["model"].invoke.call_count == 1
    args["collector"].search.assert_not_called()


def test_budget_after_fetch_keeps_link_without_starting_index(flow, monkeypatch):
    args, docs, _ = flow
    now = [0.0]
    monkeypatch.setattr(collect.time, "monotonic", lambda: now[0])
    def slow_fetch(*args, **kwargs):
        now[0] = 31.0
        return docs[0]
    args["collector"].fetch.side_effect = slow_fetch
    result = run(flow)
    assert result.partial and result.fetched_doc_ids == [docs[0].doc_id]
    assert args["doc_store"].question_links() == [("75076-1", docs[0].doc_id, docs[0].title)]
    assert result.indexed_chunks == 0 and args["embeddings"].stats["api_calls"] == 0


def test_fallback_does_not_exclude_vehicle_seed():
    candidate = Candidate("wikipedia", "en", 1, "Millennium Falcon", "fictional spacecraft", "url")
    assert collect._fallback_candidate(candidate, "역할은?")


@pytest.mark.parametrize("stage", ["search", "latest_revid", "fetch", "embedding"])
def test_external_errors_are_partial_without_sensitive_events(flow, stage):
    args, _, events = flow
    error = RuntimeError("private prompt and key")
    if stage == "embedding":
        args["embeddings"].embed_documents = Mock(side_effect=error)
    else:
        getattr(args["collector"], stage).side_effect = error
    result = run(flow)
    assert result.partial and "private" not in str(events) and "private" not in str(result)


@pytest.mark.parametrize("stage", ["p1", "p2", "search", "embedding"])
def test_missing_key_propagates(flow, stage):
    args, _, _ = flow
    error = MissingApiKey("missing")
    if stage in {"p1", "p2"}:
        args["model"] = FakeModel([error] if stage == "p1" else [p1("Gunship"), error])
    elif stage == "search":
        args["collector"].search.side_effect = error
    else:
        args["embeddings"].embed_documents = Mock(side_effect=error)
    with pytest.raises(MissingApiKey):
        run(flow)


def test_korean_links_do_not_consume_original_document_limit(flow):
    args, docs, _ = flow
    args["settings"]["collection"]["question_include_korean"] = True
    korean = replace(docs[0], doc_id="wp-ko:3", page_id=3, lang="ko", title="공화국 건쉽")
    korean_list = replace(docs[1], doc_id="wp-ko:4", page_id=4, lang="ko", title="스타워즈 탈것 목록")
    docs.extend([korean, korean_list])
    args["collector"].langlinks.return_value = {docs[0].title: korean.title, docs[1].title: korean_list.title}
    args["model"] = FakeModel([p1("Gunship"), p2("wp-en:1", "wp-en:2")])
    result = run(flow)
    assert not result.partial
    assert result.fetched_doc_ids == ["wp-en:1", "wp-en:2", "wp-ko:3", "wp-ko:4"]
    assert args["doc_store"].question_links() == [
        ("75076-1", "wp-en:1", docs[0].title), ("75076-1", "wp-en:2", docs[1].title),
        ("75076-1", "wp-ko:3", docs[0].title), ("75076-1", "wp-ko:4", docs[1].title)]
    assert {m["lang"] for m in args["docs_store"].get()["metadatas"]} == {"en", "ko"}
    assert {m["entity_kind"] for m in args["docs_store"].get()["metadatas"]} == {"question", "list"}
    calls = len(args["collector"].mock_calls)
    assert not run(flow).partial
    assert len(args["collector"].mock_calls) == calls


def test_p1_cache_separates_question_set_and_version(flow):
    args, _, _ = flow
    args["model"] = FakeModel([p1("Gunship"), p2(), p1("Gunship"), p1("Gunship"), p1("Gunship")])
    run(flow)
    run(flow, question="다른 질문")
    args["settings"]["collection"]["p1_prompt_version"] = "p1-v1"
    run(flow)
    collect.collect_for_question("A", "영화에서 어떤 역할?", **args)
    assert len(args["model"].messages) == 5
    with sqlite3.connect(args["doc_store"].collect_sqlite) as conn:
        assert conn.execute("SELECT COUNT(*) FROM llm_cache").fetchone()[0] == 4


@pytest.mark.parametrize("table", [False, True])
def test_cli_success_serializes_result_offline(flow, monkeypatch, capsys, table):
    args, _, _ = flow
    module = load_script("ask_collect")
    with sqlite3.connect(args["settings"]["paths"]["sqlite"]) as target:
        args["conn"].commit()
        args["conn"].backup(target)
    args["settings"]["paths"].update(docs_dir=str(args["doc_store"].docs_dir),
                                      collect_sqlite=str(args["doc_store"].collect_sqlite))
    monkeypatch.setattr(module, "load_settings", lambda: args["settings"])
    monkeypatch.setattr(module, "get_chat_model", lambda settings: args["model"])
    monkeypatch.setattr(module, "get_embeddings", lambda *a, **kw: args["embeddings"].inner)
    monkeypatch.setattr("rag_service.data_prep.collectors.wikipedia.WikipediaCollector", lambda *a: args["collector"])
    argv = ["--set-num", "75076-1", "--question", "역할은?", "--p1-version", "p1-v2.1"]
    assert module.main(argv + (["--table"] if table else [])) == 0
    output = capsys.readouterr()
    if table:
        assert "| 검색어 | 선택(제목·사유) | 제외(제목·사유) |" in output.out
        assert "Republic attack gunship (wp-en:1) — 질문 관련 기체" in output.out
        assert "List of Star Wars vehicles (wp-en:2) — 선별되지 않음" in output.out
        return
    result = json.loads(output.out)
    assert result["queries"][0]["q"] == "Republic attack gunship"
    assert result["indexed_chunks"] > 0 and result["selected"] == [["wp-en:1", "질문 관련 기체"]]
    assert result["selected_titles"] == {"wp-en:1": "Republic attack gunship"}
    assert result["rejected"] == [["wp-en:2", "List of Star Wars vehicles", "선별되지 않음"]]
    assert [json.loads(line)["event"] for line in output.err.splitlines()][-1] == "done"


@pytest.mark.parametrize("arguments,code", [(["--help"], 0),
    (["--set-num", "75076-1", "--question", "역할"], 2)])
def test_cli_help_and_missing_key(arguments, code):
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHON_DOTENV_DISABLED="1")
    env.pop("GMS_KEY", None)
    script = project_root() / "scripts/ask_collect.py"
    bootstrap = (
        "import sys, runpy; "
        "sys.addaudithook(lambda event,args: "
        "(_ for _ in ()).throw(AssertionError('offline')) "
        "if event in ('socket.connect','socket.getaddrinfo') or "
        "(event=='open' and isinstance(args[0],str) and args[0].replace(chr(92),'/').split('/')[-1]=='.env') else None); "
        f"runpy.run_path({str(script)!r}, run_name='__main__')"
    )
    result = subprocess.run([sys.executable, "-c", bootstrap, *arguments], env=env,
                            capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == code and result.stderr == ""
    assert ("--p1-version" if code == 0 else "GMS_KEY") in result.stdout
