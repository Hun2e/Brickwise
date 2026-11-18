"""평가용 의존성과 운영 서비스와 같은 질문 시점 수집 연결."""

import os
import sqlite3
from contextlib import contextmanager
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Callable, Iterator

from rag_service.config import resolve_path
from rag_service.llm import get_chat_model, get_embeddings
from rag_service.rag.graph import RagDeps
from rag_service.services.orchestrator import OrchestratorDeps
from rag_service.planner.collections import CollectionStore


@contextmanager
def temporary_collections(cases: list[dict]) -> Iterator[tuple[CollectionStore, dict[str, str]]]:
    """측정 종료 시 삭제되는 user DB에 문항별 컬렉션을 만든다."""
    with TemporaryDirectory(prefix="rag-eval-latency-") as directory:
        store = CollectionStore(Path(directory) / "user.sqlite")
        ids = {}
        for case in cases:
            if case.get("collection_sets"):
                cid = store.create()
                store.set_sets(cid, case["collection_sets"])
                ids[case["id"]] = cid
        yield store, ids


@contextmanager
def live_rag_dependencies(settings: dict, conn: sqlite3.Connection, model: object,
                          on_collect: Callable[[object | None], None]) -> Iterator[RagDeps]:
    """graph.default_deps와 같은 팩토리에 측정 조건 설정을 전달한다."""
    from rag_service.data_prep.collectors.http import HttpClient, build_user_agent
    from rag_service.data_prep.collectors.wikipedia import WikipediaCollector
    from rag_service.data_prep.doc_store import DocStore
    from rag_service.rag.collect import collect_for_question
    from rag_service.vectorstore.embed_cache import CachedEmbeddings
    from rag_service.vectorstore.store import get_collection, get_docs_collection

    inner = get_embeddings(settings, chunk_size=settings["index"]["embed_batch_size"],
                           check_embedding_ctx_length=False)
    contact = os.environ.get("WIKI_CONTACT", "").strip() or None
    api, app = settings["external_api"], settings["app"]
    interval = api["request_interval_sec"] if contact else api["interval_without_contact_sec"]
    client = HttpClient(build_user_agent(app["name"], app["version"], contact), interval,
                        api["max_retries"], api["timeout_sec"], api["backoff_base_sec"])
    try:
        doc_store = DocStore(resolve_path(settings["paths"]["docs_dir"]),
                             resolve_path(settings["paths"]["collect_sqlite"]))
        embeddings = CachedEmbeddings(inner, resolve_path(settings["paths"]["embed_cache"]),
                                      settings["llm"]["embedding_model"], settings["index"]["embed_batch_size"])
        meta = get_collection(settings["collections"]["items"], embeddings, settings)
        docs = get_docs_collection(embeddings, settings)
        collect = partial(collect_for_question, settings=settings, conn=conn, model=model,
                          collector=WikipediaCollector(client, settings), doc_store=doc_store,
                          docs_store=docs, embeddings=embeddings)

        def observed_collect(*args: object, **kwargs: object) -> object:
            try:
                result = collect(*args, **kwargs)
            except Exception:
                on_collect(None)
                raise
            on_collect(result)
            return result

        yield RagDeps(settings, model, meta, docs, observed_collect)
    finally:
        client.session.close()


def stores(client: object, settings: dict) -> tuple[object, object]:
    from langchain_chroma import Chroma
    from rag_service.vectorstore.embed_cache import CachedEmbeddings

    inner = get_embeddings(settings, chunk_size=settings["index"]["embed_batch_size"], check_embedding_ctx_length=False)
    embeddings = CachedEmbeddings(inner, resolve_path(settings["paths"]["embed_cache"]),
                                  settings["llm"]["embedding_model"], settings["index"]["embed_batch_size"])
    return tuple(Chroma(client=client, collection_name=settings["collections"][key],
                        embedding_function=embeddings, create_collection_if_not_exists=False)
                 for key in ("items", "docs"))


@contextmanager
def dependencies(settings: dict, *, client: object | None = None, live: bool = False,
                 on_collect: Callable[[object | None], None] | None = None) -> Iterator[OrchestratorDeps]:
    if live and client is not None:
        raise ValueError("live와 snapshot은 동시에 사용할 수 없습니다.")
    model = get_chat_model(settings)
    path = resolve_path(settings["paths"]["sqlite"])
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        deps = OrchestratorDeps(conn=conn, settings=settings, model=model)
        if client is not None:
            meta, docs = stores(client, settings)
            deps.meta_store = meta
            deps.rag_deps = RagDeps(settings=settings, model=model, meta_store=meta, docs_store=docs, collect_fn=None)
        elif live:
            # 기본 graph 팩토리는 설정을 다시 읽어 cold/override를 잃는다.
            # 서비스처럼 RAG 경로가 실행될 때만 자원을 열고 호출 안에서 닫는다.
            def live_ask(*args: object, **kwargs: object) -> dict:
                from rag_service.rag.graph import ask

                with live_rag_dependencies(settings, conn, model, on_collect or (lambda _: None)) as rag:
                    return ask(*args, **{**kwargs, "deps": rag})
            deps.ask_fn = live_ask
        yield deps
    finally:
        conn.close()
