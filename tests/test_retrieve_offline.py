"""가짜 수집기 및 로컬 Chroma로 검색·상한·중복을 검증한다."""

from dataclasses import replace
from unittest.mock import Mock

import pytest
from langchain_core.documents import Document

from test_index_items_offline import cached, offline, settings
from retrieval_helpers import configure_similarity
from rag_service.rag.collect import CollectResult
from rag_service.rag.evidence import Card
from rag_service.rag.graph import RagDeps
from rag_service.rag.retrieve import retrieve
from rag_service.vectorstore.store import get_collection, get_docs_collection


def hit(ident, *, doc_id=None, text=None, scope="entity", kind="vehicle", score=0.1, sets=None):
    meta = dict(source_type="wikipedia", doc_id=doc_id or ident, title=f"Title {ident}",
                url=f"https://example.invalid/{ident}", license="CC BY-SA 4.0",
                scope=scope, entity_kind=kind, section_path="History", lang="en")
    if sets is not None:
        meta["sets"] = sets
    return Document(id=ident, page_content=text or f"Text {ident}", metadata=meta), score


@pytest.fixture
def deps(settings):
    configure_similarity(settings)
    meta = Mock()
    meta.get.return_value = {"ids": ["75076-1"], "documents": ["세트 DB 본문"],
                             "metadatas": [{"name": "Republic Gunship"}]}
    docs = Mock()
    docs.similarity_search_with_score.return_value = []
    collect = Mock(return_value=CollectResult("75076-1", en_terms=["Gunship", "battle"]))
    return RagDeps(settings=settings, meta_store=meta, docs_store=docs, collect_fn=collect)


def run(deps, **state):
    return retrieve({"set_num": "75076-1", "question": "역할?", **state}, deps)


def test_meta_is_direct_and_always_present(deps):
    result = run(deps)
    meta, = result["cards"]
    assert (meta.n, meta.source_type, meta.title) == (1, "set_meta", "Republic Gunship")
    assert meta.url == "https://rebrickable.com/sets/75076-1/" and meta.license == "Rebrickable"
    deps.meta_store.get.assert_called_once_with(ids=["75076-1"], include=["documents", "metadatas"])
    deps.docs_store.similarity_search_with_score.assert_called_once_with(
        "역할? Gunship battle", k=20,
        filter={"$or": [{"sets": {"$contains": "75076-1"}}, {"scope": "series"}]})


@pytest.mark.parametrize("kind,scope", [("vehicle", "series"), ("group", "entity"), ("list", "entity")])
def test_category_caps_do_not_backfill(deps, kind, scope):
    deps.docs_store.similarity_search_with_score.return_value = [
        hit(str(i), kind=kind, scope=scope) for i in range(6)]
    cards = run(deps)["cards"]
    assert len(cards) == 3 and [c.chunk_id for c in cards[1:]] == ["0", "1"]


def test_per_doc_caps_scores_and_top_k(deps):
    hits = [hit(str(i), doc_id="same", score=i) for i in range(3)]
    hits += [hit(str(i), score=i) for i in range(3, 9)]
    deps.docs_store.similarity_search_with_score.return_value = list(reversed(hits))
    cards = run(deps)["cards"]
    assert [c.chunk_id for c in cards[1:]] == ["0", "1", "3", "4", "5"]
    assert [c.score for c in cards[1:]] == [0, 1, 3, 4, 5]


def test_dedup_order_and_no_input_mutation(deps):
    injected = Card(99, "db", "DB 결과", "url", "Rebrickable", "DB 수치", chunk_id="db:one")
    deps.docs_store.similarity_search_with_score.return_value = [
        hit("duplicate", text="DB  수치"), hit("a", text="English   text"),
        hit("a", text="other text"), hit("b", text="English\ntext"),
        hit("ko", text="한국어 본문"), hit("meta-copy", text="세트 DB 본문")]
    cards = run(deps, injected_cards=[injected, injected])["cards"]
    assert [c.chunk_id for c in cards] == ["db:one", "75076-1", "a", "ko"]
    assert [c.n for c in cards] == [1, 2, 3, 4] and injected.n == 99


def test_duplicate_injection_cannot_displace_required_meta(deps):
    injected = Card(99, "db", "복사", "", "", "세트 DB 본문", chunk_id="copy")
    assert [c.source_type for c in run(deps, injected_cards=[injected])["cards"]] == ["set_meta"]


@pytest.mark.parametrize("failure", [False, True])
def test_p1_fallback_partial_and_exception(deps, failure):
    if failure:
        deps.collect_fn.side_effect = RuntimeError("private-key-full-prompt")
    else:
        deps.collect_fn.return_value = CollectResult("75076-1", partial=True)
    result = run(deps, warnings=["이전 경고"])
    assert deps.docs_store.similarity_search_with_score.call_args.args == ("역할? Republic Gunship",)
    assert len(result["warnings"]) == 2 and "private-key" not in repr(result)
    assert result["cards"][0].source_type == "set_meta"


def test_expansion_and_fetch_settings(deps):
    deps.settings["retrieval"].update(max_en_terms=2, top_k=3, similarity_fetch_k=21)
    deps.collect_fn.return_value.en_terms = [" ", " one ", "two", "three"]
    run(deps)
    call = deps.docs_store.similarity_search_with_score.call_args
    assert call.args == ("역할? one two",) and call.kwargs["k"] == 21


def test_missing_meta_has_actionable_warning(deps):
    deps.meta_store.get.return_value = {"ids": [], "documents": [], "metadatas": []}
    result = run(deps)
    assert not result["cards"] and "index_items.py" in result["warnings"][0]


def test_real_chroma_filter_and_chunk_ids(tmp_path, deps):
    embeddings = cached(tmp_path)
    deps.meta_store = get_collection("set_meta", embeddings, deps.settings)
    deps.docs_store = get_docs_collection(embeddings, deps.settings)
    deps.meta_store.add_texts(["세트 DB 본문"], metadatas=[{"name": "Republic Gunship"}], ids=["75076-1"])
    docs = [hit("linked", sets=["75076-1"])[0], hit("series", scope="series")[0],
            hit("unrelated", sets=["other"])[0], hit("unlinked")[0]]
    deps.docs_store.add_documents(docs)
    cards = run(deps)["cards"]
    assert cards[0].chunk_id == "75076-1"
    assert {card.chunk_id for card in cards[1:]} == {"linked", "series"}
    assert all(card.score is not None for card in cards[1:])
