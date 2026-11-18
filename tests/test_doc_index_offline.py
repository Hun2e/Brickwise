"""실제 임시 Chroma와 FakeEmbedding으로 RAG 2 동기화를 검증한다."""

import json
import os
import subprocess
import sys
from dataclasses import replace
from unittest.mock import Mock

import pytest
from langchain_core.embeddings import DeterministicFakeEmbedding

from test_index_items_offline import cached, embedding_error, load_script, offline, settings
from test_prefetch_targets import mapping, mapped_fixture
from test_chunking import wiki_doc
from rag_service.config import project_root
from rag_service.data_prep.collectors.base import Section
from rag_service.vectorstore.doc_index import DocWithScope, doc_sets, sync_set_docs
from rag_service.vectorstore.store import get_collection, get_docs_collection


@pytest.fixture
def documents(wiki_doc):
    english = replace(wiki_doc, sections=[Section("Overview", 2, "alpha " * 400)])
    korean = replace(english, lang="ko", doc_id="wp-ko:99", page_id=99,
                      title="한국어", sections=[Section("개요", 2, "한국어 본문 " * 100)])
    series = replace(english, doc_id="wp-en:100", page_id=100, title="Series",
                     sections=[Section("Series", 2, "series " * 100)])
    return [DocWithScope(english, "entity", "Falcon", "vehicle"),
            DocWithScope(korean, "entity", "Falcon", "vehicle"),
            DocWithScope(series, "series", None, "series")]


def test_whole_mapping_preserves_other_set(mapping):
    assert doc_sets(mapping)["Millennium Falcon"] == ["A", "B"]
    mapping.execute("DELETE FROM set_entities WHERE set_num='A'")
    mapping.execute("INSERT INTO set_entities VALUES ('A', 'Millennium Falcon', 'vehicle', 0)")
    assert doc_sets(mapping)["Millennium Falcon"] == ["A", "B"]
    mapping.execute("DELETE FROM set_entities WHERE set_num='A'")
    assert doc_sets(mapping)["Millennium Falcon"] == ["B"]


def test_idempotent_shared_sets_korean_series_and_filters(tmp_path, settings, documents):
    cache = cached(tmp_path)
    store = get_docs_collection(cache, settings)
    meta = get_collection(settings["collections"]["items"], cache, settings)
    meta.add_texts(["metadata"], ids=["A"])
    links = {"Falcon": ["B", "A", "A"]}
    first = sync_set_docs(store, documents, links, settings["chunking"])
    assert first["documents"] == first["new"] == 3 and first["api_calls"] > 0
    data = store.get(include=["metadatas", "documents"])
    assert len(set(data["ids"])) == first["chunks"]
    for ident, info in zip(data["ids"], data["metadatas"]):
        assert ident == f"{info['scope']}:{info['doc_id']}:{info['seq']:04d}"
        assert info.get("sets", []) == (["A", "B"] if info["scope"] == "entity" else [])
        assert {"url", "license", "attribution", "entity", "entity_kind", "revid", "collected_at",
                "section_path", "chunk_config", "chunk_config_hash"} <= info.keys()
    second = sync_set_docs(store, documents, links, settings["chunking"])
    assert second["skipped"] == 3 and second["api_calls"] == second["embedded"] == 0
    changed = sync_set_docs(store, documents, {"Falcon": ["B", "C"]}, settings["chunking"])
    assert changed["metadata_updated"] == 2 and changed["api_calls"] == 0
    assert store.get(where={"sets": {"$contains": "A"}})["ids"] == []
    hit = store.get(where={"sets": {"$contains": "B"}}, include=["metadatas"])
    assert {row["lang"] for row in hit["metadatas"]} == {"en", "ko"}
    where = {"$or": [{"sets": {"$contains": "B"}}, {"scope": "series"}]}
    assert len(store.get(where=where)["ids"]) == first["chunks"]
    unknown = {"$or": [{"sets": {"$contains": "unknown"}}, {"scope": "series"}]}
    assert all(row["scope"] == "series" for row in store.get(where=unknown)["metadatas"])
    assert meta.get()["ids"] == ["A"]


def test_revision_config_delete_and_cache_stats(tmp_path, settings, documents):
    cache = cached(tmp_path)
    store = get_docs_collection(cache, settings)
    cfg = settings["chunking"]
    sync_set_docs(store, documents, {"Falcon": ["A"]}, cfg)
    before = store.get(where={"lang": "ko"}, include=["documents", "embeddings", "metadatas"])
    changed = [replace(documents[0], doc=replace(documents[0].doc, revid=2,
                sections=[Section("Changed", 2, "new body")])), *documents[1:]]
    report = sync_set_docs(store, changed, {"Falcon": ["A"]}, cfg)
    assert report["updated"] == 1 and report["skipped"] == 2 and report["embedded"] == 1
    after = store.get(where={"lang": "ko"}, include=["documents", "embeddings", "metadatas"])
    assert before["metadatas"] == after["metadatas"] and before["documents"] == after["documents"]
    assert (before["embeddings"] == after["embeddings"]).all()
    assert len(store.get(where={"doc_id": documents[0].doc.doc_id})["ids"]) == 1
    revision_only = [replace(changed[0], doc=replace(changed[0].doc, revid=3)), *changed[1:]]
    report = sync_set_docs(store, revision_only, {"Falcon": ["A"]}, cfg)
    assert report["updated"] == 1 and report["api_calls"] == 0 and report["cache_hits"] == 1
    report = sync_set_docs(store, revision_only, {"Falcon": ["A"]}, {**cfg, "header": False})
    assert report["updated"] == 3 and report["embedded"] > 0
    report = sync_set_docs(store, revision_only[:1], {"Falcon": ["A"]}, {**cfg, "header": False})
    assert report["deleted"] == 2 and report["deleted_chunks"] > 0
    assert report["chunks"] == 1
    assert sync_set_docs(store, [], {}, cfg)["chunks"] == 0


def test_duplicate_doc_alias_union_and_removing_sets(tmp_path, settings, documents):
    store = get_docs_collection(cached(tmp_path), settings)
    doc = documents[0]
    aliases = [doc, replace(doc, entity="Alias")]
    report = sync_set_docs(store, aliases, {"Falcon": ["A"], "Alias": ["B"]}, settings["chunking"])
    assert report["documents"] == 1
    assert all(row["sets"] == ["A", "B"] for row in store.get()["metadatas"])
    report = sync_set_docs(store, aliases, {}, settings["chunking"])
    assert report["metadata_updated"] == 1 and report["api_calls"] == 0
    assert all("sets" not in row for row in store.get()["metadatas"])
    assert store.get(where={"sets": {"$contains": "A"}})["ids"] == []


def test_failed_embeddings_preserve_previous_docs(tmp_path, settings, documents, monkeypatch):
    cache = cached(tmp_path)
    store = get_docs_collection(cache, settings)
    sync_set_docs(store, documents, {}, settings["chunking"])
    before = store.get(include=["documents", "metadatas"])
    monkeypatch.setattr(cache, "embed_documents", Mock(side_effect=RuntimeError("offline failure")))
    changed = replace(documents[0], doc=replace(documents[0].doc, revid=2))
    with pytest.raises(RuntimeError):
        sync_set_docs(store, [changed], {}, settings["chunking"])
    assert store.get(include=["documents", "metadatas"]) == before


@pytest.mark.parametrize("metadata_only_first", [False, True])
@pytest.mark.parametrize("transient", [False, True])
def test_late_batch_failure_preserves_entire_collection_and_reuses_cache(
    tmp_path, settings, documents, metadata_only_first, transient, monkeypatch,
):
    monkeypatch.setattr("rag_service.vectorstore.embed_cache.time.sleep", Mock())
    cache = cached(tmp_path, batch_size=1)
    store = get_docs_collection(cache, settings)
    sync_set_docs(store, documents, {"Falcon": ["A"]}, settings["chunking"])
    before = store.get(include=["documents", "metadatas", "embeddings"])
    changed = [replace(item, doc=replace(item.doc, revid=2,
               sections=[Section("Changed", 2, "fresh body " + item.doc.doc_id)]))
               for item in documents[:2]]
    if metadata_only_first:
        changed[0] = documents[0]
        # 두 번째 문서 안의 여러 배치 중 실패해도 앞 메타 변경은 보류해야 한다.
        changed[1] = replace(changed[1], doc=replace(changed[1].doc, sections=[
            Section("Changed", 2, " ".join(f"fresh-{i}" for i in range(500)))]))
    inner = cache.inner
    calls = []
    cfg = settings["chunking"] if metadata_only_first else {**settings["chunking"], "header": False}
    failure = embedding_error(502) if transient else RuntimeError("second batch failed")

    def failing(texts):
        calls.append(texts)
        if len(calls) >= 2:
            raise failure
        return inner.embed_documents(texts)

    cache.inner = Mock(embed_documents=Mock(side_effect=failing))
    with pytest.raises(type(failure)) as caught:
        sync_set_docs(store, changed, {"Falcon": ["B"]}, cfg)
    assert caught.value is failure
    assert len(calls) == 2 + (cache.max_retries if transient else 0)
    after = store.get(include=["documents", "metadatas", "embeddings"])
    assert after["ids"] == before["ids"]
    assert after["documents"] == before["documents"]
    assert after["metadatas"] == before["metadatas"]
    assert (after["embeddings"] == before["embeddings"]).all()
    retry = cached(tmp_path, batch_size=1)
    retry.inner = Mock(wraps=inner)
    report = sync_set_docs(store, changed, {"Falcon": ["B"]}, cfg, embeddings=retry)
    assert report["cache_hits"] >= 1 and report["deleted"] == 1
    assert calls[0] not in [call.args[0] for call in retry.inner.embed_documents.call_args_list]
    assert report["metadata_updated"] == int(metadata_only_first)
    assert all(meta["sets"] == ["B"] for meta in store.get()["metadatas"])


def test_index_cli_local_only_twice(mapping, tmp_path, settings, monkeypatch, capsys):
    collector, doc_store, config = mapped_fixture(tmp_path)
    load_script("prefetch_docs").collect_mapped(mapping, doc_store, collector, config)
    capsys.readouterr()
    import sqlite3
    with sqlite3.connect(settings["paths"]["sqlite"]) as target:
        mapping.commit()
        mapping.backup(target)
    settings["wikipedia"] = config["wikipedia"]
    settings["index"] = config["index"]
    settings["paths"].update(docs_dir=str(doc_store.docs_dir), collect_sqlite=str(doc_store.collect_sqlite))
    module = load_script("index_docs")
    monkeypatch.setattr(module, "load_settings", lambda: settings)
    monkeypatch.setattr(module, "get_embeddings", Mock(return_value=DeterministicFakeEmbedding(size=16)))
    assert module.main([]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["new"] == 2 and first["api_calls"] > 0
    assert module.main([]) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["skipped"] == 2 and second["api_calls"] == 0
    # 수집 불완전 상태에서는 전체 목록을 삭제하는 동기화를 시작하지 않는다.
    doc_store.log_fetch("wikipedia", "mapped:en:Millennium Falcon", None, None, "missing")
    assert module.main([]) == 1
    assert "prefetch_docs.py --mapped" in capsys.readouterr().err
    assert get_docs_collection(cached(tmp_path), settings).get()["ids"]


@pytest.mark.parametrize("name,args,code", [
    ("prefetch_docs", ["--help"], 0), ("index_docs", ["--help"], 0), ("index_docs", [], 2)])
def test_cli_help_and_missing_key_without_network(name, args, code):
    script = project_root() / "scripts" / f"{name}.py"
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHON_DOTENV_DISABLED="1")
    env.pop("GMS_KEY", None)
    bootstrap = (
        "import sys, runpy; "
        "sys.addaudithook(lambda event,args: "
        "(_ for _ in ()).throw(AssertionError('offline')) "
        "if event in ('socket.connect','socket.getaddrinfo') or "
        "(event=='open' and isinstance(args[0],str) and args[0].replace(chr(92),'/').split('/')[-1]=='.env') else None); "
        f"runpy.run_path({str(script)!r}, run_name='__main__')"
    )
    result = subprocess.run([sys.executable, "-c", bootstrap, *args], env=env,
                            capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == code and result.stderr == ""
    if name == "prefetch_docs":
        assert "--seeds" in result.stdout and "--mapped" in result.stdout
    else:
        assert "GMS_KEY" in result.stdout
