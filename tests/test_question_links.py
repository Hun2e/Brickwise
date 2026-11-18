"""질문 연결 합집합과 전체 재색인 보존 회귀를 검증한다."""

import json
import sqlite3
from dataclasses import replace
from unittest.mock import Mock

import pytest
from langchain_core.embeddings import DeterministicFakeEmbedding

from test_index_items_offline import cached, load_script, offline, settings
from test_prefetch_targets import mapping, mapped_fixture
from rag_service.data_prep.doc_store import DocStore
from rag_service.vectorstore.doc_index import doc_sets, question_documents, sync_set_docs
from rag_service.vectorstore.store import get_docs_collection


def test_link_upsert_and_union(tmp_path, mapping):
    store = DocStore(tmp_path / "docs", tmp_path / "collect.sqlite")
    for number in ("B", "D", "D"):
        store.link_question_doc(number, "wp-en:1", "Millennium Falcon", "en", "query", "reason")
    assert store.question_links() == [("B", "wp-en:1", "Millennium Falcon"), ("D", "wp-en:1", "Millennium Falcon")]
    assert doc_sets(mapping, store)["Millennium Falcon"] == ["A", "B", "D"]
    mapping.execute("DELETE FROM set_entities WHERE set_num='A'")
    assert doc_sets(mapping, store)["Millennium Falcon"] == ["B", "D"]
    assert doc_sets(mapping)["Millennium Falcon"] == ["B"]


def test_index_cli_preserves_question_documents(mapping, tmp_path, settings, monkeypatch, capsys):
    collector, doc_store, config = mapped_fixture(tmp_path)
    load_script("prefetch_docs").collect_mapped(mapping, doc_store, collector, config)
    capsys.readouterr()
    with sqlite3.connect(settings["paths"]["sqlite"]) as target:
        mapping.commit()
        mapping.backup(target)
    settings["wikipedia"] = config["wikipedia"]
    settings["index"] = config["index"]
    settings["paths"].update(docs_dir=str(doc_store.docs_dir), collect_sqlite=str(doc_store.collect_sqlite))
    base = collector.fetch("en", collector.latest_revid("en", "Millennium Falcon")[0])
    question = replace(base, doc_id="wp-en:500", page_id=500, title="List of Star Wars vehicles")
    korean = replace(question, doc_id="wp-ko:501", page_id=501, lang="ko", title="스타워즈 탈것 목록")
    for doc in (question, korean):
        doc_store.save(doc)
        doc_store.link_question_doc("D", doc.doc_id, question.title, doc.lang, "vehicles", "relevant")
    doc_store.link_question_doc("D", base.doc_id, "Millennium Falcon", "en", "Falcon", "relevant")
    embeddings = cached(tmp_path)
    store = get_docs_collection(embeddings, settings)
    sync_set_docs(store, question_documents(doc_store), doc_sets(mapping, doc_store), settings["chunking"])
    old_ids = set(store.get(where={"doc_id": question.doc_id})["ids"])
    module = load_script("index_docs")
    monkeypatch.setattr(module, "load_settings", lambda: settings)
    monkeypatch.setattr(module, "get_embeddings", Mock(return_value=DeterministicFakeEmbedding(size=16)))
    for _ in range(2):
        assert module.main([]) == 0
        assert json.loads(capsys.readouterr().out)["deleted"] == 0
        assert set(store.get(where={"doc_id": question.doc_id})["ids"]) == old_ids
        assert all(m["sets"] == ["D"] and m["entity_kind"] == "list"
                   for m in store.get(where={"doc_id": korean.doc_id})["metadatas"])
        assert all(m["sets"] == ["A", "B", "D"] and m["entity_kind"] == "vehicle"
                   for m in store.get(where={"doc_id": base.doc_id})["metadatas"])
    question_path = doc_store.docs_dir / "wikipedia" / "en" / "500.json"
    question_path.unlink()
    assert module.main([]) == 1
    assert set(store.get(where={"doc_id": question.doc_id})["ids"]) == old_ids


def test_llm_cache_is_persistent_and_versioned(tmp_path):
    store = DocStore(tmp_path / "docs", tmp_path / "collect.sqlite")
    store.save_llm_cache("hash", "p1-v2", '{"queries": []}')
    reopened = DocStore(store.docs_dir, store.collect_sqlite)
    assert reopened.get_llm_cache("hash", "p1-v2") == '{"queries": []}'
    assert reopened.get_llm_cache("hash", "p1-v1") is None
