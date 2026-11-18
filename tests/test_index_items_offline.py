"""네트워크·비밀 파일 없이 캐시와 실제 로컬 Chroma를 검증한다."""

import builtins
import hashlib
import importlib.util
import io
import json
import os
import socket
import sqlite3
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from langchain_core.embeddings import DeterministicFakeEmbedding
from pydantic_settings import DotEnvSettingsSource

from rag_service import llm
from rag_service.config import load_settings, project_root
from rag_service.data_prep.item_text import ItemDoc, iter_item_docs
from rag_service.vectorstore.embed_cache import CachedEmbeddings
from test_entities import real_db

# Chroma는 import 시에도 Settings(env_file='.env')를 생성한다.
# 테스트에서는 제3자 dotenv 소스를 비워 실제 비밀 파일 탐색·열기를 막는다.
with patch.object(DotEnvSettingsSource, "_read_env_files", return_value={}):
    from rag_service.vectorstore.store import get_collection, sync_set_meta


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Network access is forbidden")

    def guard(original):
        def checked(file, *args, **kwargs):
            if isinstance(file, (str, Path)):
                assert Path(file).name != ".env", ".env access is forbidden"
            return original(file, *args, **kwargs)
        return checked

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(builtins, "open", guard(builtins.open))
    monkeypatch.setattr(io, "open", guard(io.open))
    monkeypatch.delenv("GMS_KEY", raising=False)
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.setenv("ANONYMIZED_TELEMETRY", "False")
    monkeypatch.setattr(DotEnvSettingsSource, "_read_env_files", lambda self: {})
    monkeypatch.setattr(llm, "load_dotenv", Mock(return_value=False))
    llm._load_environment.cache_clear()
    yield
    llm._load_environment.cache_clear()


def cached(tmp_path, model="fake-v1", batch_size=2):
    return CachedEmbeddings(DeterministicFakeEmbedding(size=16), tmp_path / "cache.sqlite", model, batch_size)


def test_cache_batches_duplicates_order_persistence_and_model(tmp_path):
    cache = cached(tmp_path)
    texts = ["가", "나", "가", "다", "라", "마"]
    expected = cache.inner.embed_documents(texts)
    assert cache.embed_documents(texts) == expected
    assert cache.stats == {"api_calls": 3, "cache_hits": 0, "embedded": 5}
    assert cache.embed_documents(texts) == expected
    assert cache.stats == {"api_calls": 3, "cache_hits": 6, "embedded": 5}
    reopened = cached(tmp_path)
    assert reopened.embed_documents(texts) == expected
    assert reopened.stats == {"api_calls": 0, "cache_hits": 6, "embedded": 0}
    changed_model = cached(tmp_path, "fake-v2")
    assert changed_model.embed_documents(texts) == expected
    assert changed_model.stats["api_calls"] == 3
    with sqlite3.connect(tmp_path / "cache.sqlite") as conn:
        key = hashlib.sha256("fake-v1\n가".encode()).hexdigest()
        assert json.loads(conn.execute("SELECT vector FROM embeddings WHERE cache_key=?", (key,)).fetchone()[0]) == expected[0]
        assert conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0] == 10


def test_query_cache_and_empty_input(tmp_path):
    cache = cached(tmp_path)
    assert cache.embed_documents([]) == []
    assert cache.stats["api_calls"] == 0
    expected = cache.inner.embed_query("질문")
    assert cache.embed_query("질문") == expected
    assert cache.embed_query("질문") == expected
    assert cache.embed_documents(["질문"]) == [expected]
    assert cache.stats == {"api_calls": 1, "cache_hits": 2, "embedded": 1}
    with pytest.raises(ValueError):
        cached(tmp_path, batch_size=0)


@pytest.mark.parametrize("response", [[], [[]], [[float("nan")]]])
def test_bad_embedding_not_cached(tmp_path, response):
    cache = cached(tmp_path)
    cache.inner = Mock(embed_documents=Mock(return_value=response))
    with pytest.raises(ValueError):
        cache.embed_documents(["x"])
    cache.inner = DeterministicFakeEmbedding(size=16)
    cache.embed_documents(["x"])
    assert cache.stats == {"api_calls": 2, "cache_hits": 0, "embedded": 1}


def test_failed_batch_keeps_successful_cache(tmp_path):
    cache = cached(tmp_path, batch_size=1)
    first = cache.inner.embed_documents(["one"])
    cache.inner = Mock(embed_documents=Mock(side_effect=[first, RuntimeError("failed")]))
    with pytest.raises(RuntimeError):
        cache.embed_documents(["one", "two"])
    retried = cached(tmp_path)
    retried.embed_documents(["one", "two"])
    assert retried.stats == {"api_calls": 1, "cache_hits": 1, "embedded": 1}


def embedding_error(kind):
    import httpx
    from openai import APIConnectionError, APIStatusError, APITimeoutError

    request = httpx.Request("POST", "https://example.invalid/embeddings")
    if kind == "connection":
        return APIConnectionError(request=request)
    if kind == "timeout":
        return APITimeoutError(request=request)
    if kind == "unexpected":
        return ValueError("invalid input")
    return APIStatusError("test error", response=httpx.Response(kind, request=request), body=None)


@pytest.mark.parametrize("kind", [429, 500, 502, 503, "connection", "timeout"])
def test_transient_embedding_retries_only_failed_batch(tmp_path, settings, monkeypatch, kind):
    settings["index"].update(embed_max_retries=2, embed_retry_backoff_sec=0.25)
    sleep = Mock()
    monkeypatch.setattr("rag_service.vectorstore.embed_cache.time.sleep", sleep)
    inner = Mock(embed_documents=Mock(side_effect=[[[1.0]], embedding_error(kind), [[2.0]]]))
    cache = CachedEmbeddings(inner, tmp_path / "retry.sqlite", "fake", 1, settings=settings)
    assert cache.embed_documents(["one", "two"]) == [[1.0], [2.0]]
    assert [call.args[0] for call in inner.embed_documents.call_args_list] == [["one"], ["two"], ["two"]]
    sleep.assert_called_once_with(0.25)
    assert cache.stats == {"api_calls": 3, "cache_hits": 0, "embedded": 2}
    assert cache.embed_documents(["one", "two"]) == [[1.0], [2.0]]
    assert inner.embed_documents.call_count == 3


@pytest.mark.parametrize("kind", [400, 401, 403, 404, 422, "unexpected"])
def test_permanent_embedding_errors_do_not_retry(tmp_path, settings, monkeypatch, kind):
    sleep = Mock()
    monkeypatch.setattr("rag_service.vectorstore.embed_cache.time.sleep", sleep)
    error = embedding_error(kind)
    inner = Mock(embed_documents=Mock(side_effect=error))
    cache = CachedEmbeddings(inner, tmp_path / "retry.sqlite", "fake", 1, settings=settings)
    with pytest.raises(type(error)) as caught:
        cache.embed_documents(["one"])
    assert caught.value is error
    assert inner.embed_documents.call_count == 1
    sleep.assert_not_called()


@pytest.mark.parametrize("retries", [0, 1, 3])
def test_embedding_retry_limit_uses_settings(tmp_path, settings, monkeypatch, retries):
    settings["index"].update(embed_max_retries=retries, embed_retry_backoff_sec=0.25)
    sleep = Mock()
    monkeypatch.setattr("rag_service.vectorstore.embed_cache.time.sleep", sleep)
    error = embedding_error(502)
    inner = Mock(embed_documents=Mock(side_effect=error))
    cache = CachedEmbeddings(inner, tmp_path / "retry.sqlite", "fake", 1, settings=settings)
    with pytest.raises(type(error)) as caught:
        cache.embed_documents(["one"])
    assert caught.value is error and inner.embed_documents.call_count == retries + 1
    assert [call.args[0] for call in sleep.call_args_list] == [0.25 * 2 ** n for n in range(retries)]
    assert cache.stats == {"api_calls": retries + 1, "cache_hits": 0, "embedded": 0}


@pytest.mark.parametrize("header,expected", [("5", 5.0), ("invalid", 2.0)])
def test_embedding_retry_after(tmp_path, settings, monkeypatch, header, expected):
    settings["index"].update(embed_max_retries=1, embed_retry_backoff_sec=2)
    sleep = Mock()
    monkeypatch.setattr("rag_service.vectorstore.embed_cache.time.sleep", sleep)
    error = embedding_error(429)
    error.response.headers["Retry-After"] = header
    inner = Mock(embed_documents=Mock(side_effect=[error, [[1.0]]]))
    cache = CachedEmbeddings(inner, tmp_path / "retry.sqlite", "fake", 1, settings=settings)
    assert cache.embed_documents(["one"]) == [[1.0]]
    sleep.assert_called_once_with(expected)


def test_embedding_retry_after_http_date(tmp_path, settings, monkeypatch):
    from datetime import datetime, timezone
    from email.utils import format_datetime

    now = datetime(2026, 10, 23, tzinfo=timezone.utc)
    clock = Mock()
    clock.now.return_value = now
    monkeypatch.setattr("rag_service.vectorstore.embed_cache.datetime", clock)
    sleep = Mock()
    monkeypatch.setattr("rag_service.vectorstore.embed_cache.time.sleep", sleep)
    settings["index"].update(embed_max_retries=1, embed_retry_backoff_sec=2)
    error = embedding_error(503)
    error.response.headers["Retry-After"] = format_datetime(now.replace(second=7), usegmt=True)
    inner = Mock(embed_documents=Mock(side_effect=[error, [[1.0]]]))
    cache = CachedEmbeddings(inner, tmp_path / "retry.sqlite", "fake", 1, settings=settings)
    assert cache.embed_documents(["one"]) == [[1.0]]
    sleep.assert_called_once_with(7.0)


@pytest.fixture
def settings(tmp_path):
    settings = load_settings()
    settings["paths"].update(chroma_dir=str(tmp_path / "chroma"),
                              embed_cache=str(tmp_path / "cache.sqlite"),
                              sqlite=str(tmp_path / "lego.sqlite"))
    return settings


def test_chroma_upsert_delete_arrays_and_metadata_removal(tmp_path, settings):
    cache = cached(tmp_path)
    store = get_collection(settings["collections"]["items"], cache, settings)
    external = get_collection(settings["collections"]["docs"], cache, settings)
    external.add_texts(["external"], ids=["external-1"])
    docs = [ItemDoc("1-1", "first", {"source_type": "set_meta", "entities": ["Falcon", "Luke"]}),
            ItemDoc("2-1", "second", {"source_type": "set_meta", "has_ko_alias": False})]
    assert sync_set_meta(store, docs) == {"total": 2, "upserted": 2, "new": 2, "updated": 0, "deleted": 0}
    calls = cache.stats["api_calls"]
    assert sync_set_meta(store, docs) == {"total": 2, "upserted": 2, "new": 0, "updated": 2, "deleted": 0}
    assert cache.stats["api_calls"] == calls
    hit = store.get(where={"entities": {"$contains": "Falcon"}}, include=["metadatas"])
    assert hit["ids"] == ["1-1"] and hit["metadatas"][0]["entities"] == ["Falcon", "Luke"]
    # 새 프로세스에서 사용하는 것과 같은 경로의 컬렉션으로 다시 연다.
    reopened = get_collection(settings["collections"]["items"], cached(tmp_path), settings)
    assert len(reopened.get()["ids"]) == 2
    changed = replace(docs[0], text="changed", metadata={"source_type": "set_meta", "has_ko_alias": False})
    assert sync_set_meta(store, [changed])["deleted"] == 1
    data = store.get(include=["metadatas", "documents"])
    assert data["documents"] == ["changed"] and "entities" not in data["metadatas"][0]
    assert store.get(where={"entities": {"$contains": "Falcon"}})["ids"] == []
    assert sync_set_meta(store, [docs[0]])["new"] == 0
    assert store.get(where={"entities": {"$contains": "Luke"}})["ids"] == ["1-1"]
    with pytest.raises(ValueError, match="중복"):
        sync_set_meta(store, docs + docs)
    assert sync_set_meta(store, [])["total"] == 0
    assert external.get()["ids"] == ["external-1"]


def test_failed_upsert_does_not_delete_out_of_scope(tmp_path, settings, monkeypatch):
    store = get_collection("set_meta", cached(tmp_path), settings)
    store.add_texts(["keep"], ids=["old"])
    monkeypatch.setattr(store, "add_texts", Mock(side_effect=RuntimeError("offline failure")))
    with pytest.raises(RuntimeError):
        sync_set_meta(store, [ItemDoc("new", "new", {"source_type": "set_meta"})])
    assert store.get()["ids"] == ["old"]


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, project_root() / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_real_docs_index_cli_twice(real_db, tmp_path, settings, monkeypatch, capsys):
    module = load_script("index_items")
    with sqlite3.connect(settings["paths"]["sqlite"]) as target:
        real_db.backup(target)
    monkeypatch.setattr(module, "load_settings", lambda: settings)
    monkeypatch.setenv("GMS_KEY", "offline-placeholder")
    factory = Mock(return_value=DeterministicFakeEmbedding(size=16))
    monkeypatch.setattr(llm, "OpenAIEmbeddings", factory)
    assert module.main([]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["total"] == first["new"] == first["embedded"] == 378
    assert first["api_calls"] == 6 and first["cache_hits"] == 0
    assert module.main([]) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["total"] == second["updated"] == second["cache_hits"] == 378
    assert second["api_calls"] == second["new"] == second["deleted"] == 0
    options = factory.call_args.kwargs
    assert options["model"] == settings["llm"]["embedding_model"]
    assert options["base_url"] == settings["llm"]["base_url"]
    assert options["chunk_size"] == settings["index"]["embed_batch_size"]
    assert not options["check_embedding_ctx_length"]
    llm.load_dotenv.assert_called_once_with(project_root() / ".env", override=False)
    store = get_collection(settings["collections"]["items"], cached(tmp_path), settings)
    assert len(store.get()["ids"]) == len(list(iter_item_docs(real_db, ["core", "compare_only"])))


def test_cli_help_and_missing_key_subprocess():
    script = project_root() / "scripts/index_items.py"
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
    env.pop("GMS_KEY", None)
    bootstrap = (
        "import sys, runpy, dotenv; "
        "dotenv.load_dotenv=lambda *a, **k: False; "
        "sys.addaudithook(lambda event,args: "
        "(_ for _ in ()).throw(AssertionError('offline')) "
        "if event in ('socket.connect','socket.getaddrinfo') or "
        "(event=='open' and isinstance(args[0],str) and args[0].replace(chr(92),'/').split('/')[-1]=='.env') else None); "
        f"runpy.run_path({str(script)!r}, run_name='__main__')"
    )
    for args, code in [(["--help"], 0), ([], 2)]:
        result = subprocess.run([sys.executable, "-c", bootstrap, *args], env=env,
                                capture_output=True, text=True, encoding="utf-8", timeout=30)
        assert result.returncode == code and result.stderr == ""
        assert "GMS_KEY" in result.stdout and "Traceback" not in result.stdout


def test_cli_missing_db_and_sanitized_error(tmp_path, settings, monkeypatch, capsys):
    module = load_script("index_items")
    monkeypatch.setattr(module, "load_settings", lambda: settings)
    monkeypatch.setenv("GMS_KEY", "offline-placeholder")
    monkeypatch.setattr(llm, "OpenAIEmbeddings", Mock())
    assert module.main([]) == 2
    assert "세트 DB가 없습니다" in capsys.readouterr().out
    monkeypatch.setattr(module, "load_settings", Mock(side_effect=RuntimeError("private-key-full-prompt")))
    assert module.main([]) == 1
    output = capsys.readouterr()
    assert "색인 실패" in output.err and "private-key" not in output.err


def test_cli_loads_environment_before_key_check(settings, monkeypatch, capsys):
    module = load_script("index_items")
    monkeypatch.setattr(module, "load_settings", lambda: settings)

    def load_fake_environment(*args, **kwargs):
        monkeypatch.setenv("GMS_KEY", "offline-placeholder")

    loader = Mock(side_effect=load_fake_environment)
    monkeypatch.setattr(llm, "load_dotenv", loader)
    factory = Mock()
    monkeypatch.setattr(llm, "OpenAIEmbeddings", factory)
    # DB가 없어도 환경 로드와 클라이언트 구성은 완료되어야 한다.
    assert module.main([]) == 2
    assert "세트 DB가 없습니다" in capsys.readouterr().out
    loader.assert_called_once_with(project_root() / ".env", override=False)
    assert factory.call_args.kwargs["api_key"] == "offline-placeholder"


def test_cli_missing_key_after_environment_load(settings, monkeypatch, capsys):
    module = load_script("index_items")
    monkeypatch.setattr(module, "load_settings", lambda: settings)
    factory = Mock()
    monkeypatch.setattr(llm, "OpenAIEmbeddings", factory)
    assert module.main([]) == 2
    output = capsys.readouterr()
    assert output.out.strip() == "GMS_KEY가 설정되지 않았습니다. 환경변수에 키를 설정한 뒤 다시 실행하세요."
    assert output.err == ""
    llm.load_dotenv.assert_called_once_with(project_root() / ".env", override=False)
    factory.assert_not_called()


@pytest.mark.parametrize("explicit_settings", [False, True])
def test_embeddings_default_call_and_optional_overrides(settings, monkeypatch, explicit_settings):
    monkeypatch.setenv("GMS_KEY", "offline-placeholder")
    monkeypatch.setattr(llm, "load_settings", lambda: settings)
    factory = Mock()
    monkeypatch.setattr(llm, "OpenAIEmbeddings", factory)
    args = (settings,) if explicit_settings else ()
    assert llm.get_embeddings(*args) is factory.return_value
    expected = {
        "model": settings["llm"]["embedding_model"],
        "api_key": "offline-placeholder",
        "base_url": settings["llm"]["base_url"],
        "timeout": settings["llm"]["timeout_sec"],
        "max_retries": 0,
    }
    factory.assert_called_with(**expected)
    llm.get_embeddings(*args, chunk_size=7, check_embedding_ctx_length=False)
    factory.assert_called_with(**expected, chunk_size=7, check_embedding_ctx_length=False)
    llm.get_embeddings(*args)
    factory.assert_called_with(**expected)
    llm.load_dotenv.assert_called_once_with(project_root() / ".env", override=False)
