"""ADR-017 운영 기본값이 검색부터 공통 서비스까지 적용되는지 검증한다."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from rag_service.config import load_settings
from rag_service.rag.collect import CollectResult
from rag_service.rag.graph import RagDeps, ask
from rag_service.rag.retrieve import rank_docs, retrieve
from rag_service.services.orchestrator import OrchestratorDeps, answer
from test_db_queries import conn
from test_graph_offline import FakeModel
from test_index_items_offline import offline
from test_orchestrator_offline import model
from test_retrieve_offline import hit


@pytest.fixture
def default_deps(tmp_path):
    """검색 설정은 load_settings 그대로 두고 외부 의존성만 대체한다."""
    settings = load_settings()
    settings["paths"].update(docs_dir=str(tmp_path / "docs"),
                             collect_sqlite=str(tmp_path / "collect.sqlite"))
    settings["router"]["error_log"] = str(tmp_path / "orchestrator.log")
    settings["verify"]["log_path"] = str(tmp_path / "verification.jsonl")
    meta = Mock(spec=["get"])
    meta.get.return_value = {"ids": ["A-1"], "documents": ["세트 DB 본문"],
                             "metadatas": [{"name": "English set title"}]}
    docs = Mock(spec=["embeddings", "max_marginal_relevance_search_by_vector", "get", "_collection"])
    docs.embeddings = Mock(spec=["embed_query"])
    docs.embeddings.embed_query.return_value = [1., 0.]
    # 같은 문서·series의 다섯 청크로 문서별/유형별 상한 미적용을 확인한다.
    docs.max_marginal_relevance_search_by_vector.return_value = [
        hit(str(i), doc_id="same", scope="series")[0] for i in range(5)]
    docs.get.return_value = {"ids": [str(i) for i in reversed(range(5))],
                             "embeddings": [[float(i), 1.] for i in reversed(range(5))]}
    docs._collection = SimpleNamespace(configuration={"hnsw": {"space": "l2"}}, metadata=None)
    collect = Mock(return_value=CollectResult("A-1", en_terms=["English", "expansion"]))
    return RagDeps(settings, FakeModel("설명 [근거 6]"), meta, docs, collect)


def assert_default_search(deps: RagDeps, question: str) -> None:
    """질문 원문과 확정된 MMR 인자가 실제 저장소 호출에 전달되어야 한다."""
    deps.docs_store.embeddings.embed_query.assert_called_once_with(question)
    deps.docs_store.max_marginal_relevance_search_by_vector.assert_called_once_with(
        [1., 0.], k=5, fetch_k=40, lambda_mult=.7,
        filter={"$or": [{"sets": {"$contains": "A-1"}}, {"scope": "series"}]})
    deps.docs_store.get.assert_called_once_with(
        ids=[str(i) for i in range(5)], include=["embeddings"])


def test_rank_docs_uses_loaded_mmr_question_defaults(default_deps):
    deps = default_deps
    rows = rank_docs("어떤 역할?", "A-1", docs_store=deps.docs_store,
                     settings=deps.settings, en_terms=["English", "expansion"])
    assert [(row["chunk_id"], score) for row, score in rows] == [
        ("0", 2.), ("1", 1.), ("2", 2.), ("3", 5.), ("4", 10.)]
    assert_default_search(deps, "어떤 역할?")


def test_retrieve_uses_loaded_mmr_question_defaults(default_deps):
    deps = default_deps
    result = retrieve({"set_num": "A-1", "question": "어떤 역할?"}, deps)
    assert [card.chunk_id for card in result["cards"]] == ["A-1", "0", "1", "2", "3", "4"]
    assert [card.score for card in result["cards"][1:]] == [2., 1., 2., 5., 10.]
    assert not result["warnings"]
    deps.collect_fn.assert_called_once_with("A-1", "어떤 역할?", on_event=None)
    assert_default_search(deps, "어떤 역할?")


def test_graph_uses_loaded_mmr_question_defaults(default_deps):
    deps = default_deps
    result = ask("A-1", "어떤 역할?", deps=deps)
    assert result["answer"] == "설명 [근거 6]"
    assert [card.chunk_id for card in result["cards"]] == ["A-1", "0", "1", "2", "3", "4"]
    assert [card.chunk_id for card in result["used_cards"]] == ["4"]
    assert not result["warnings"] and not result["no_citation"]
    assert len(deps.model.messages) == 1
    assert "Text 4" in deps.model.messages[0][-1].content
    assert_default_search(deps, "어떤 역할?")


def test_orchestrator_uses_loaded_mmr_question_defaults(conn, default_deps):
    deps = OrchestratorDeps(conn, default_deps.settings,
                            model=model("narrative", numbers=["A"]), rag_deps=default_deps)
    result = answer("어떤 역할?", deps=deps)
    assert result.route == "narrative" and result.set_nums == ["A-1"]
    assert result.answer == "설명 [근거 6]" and result.needs_input is None
    assert [card.chunk_id for card in result.cards] == ["4"]
    assert not result.warnings
    assert len(default_deps.model.messages) == 1
    assert_default_search(default_deps, "어떤 역할?")
