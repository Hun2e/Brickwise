"""retrieve → generate 두 노드 그래프와 공통 질문 진입점."""

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Callable, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from rag_service.config import load_settings, resolve_path
from rag_service.rag.collect import CollectResult
from rag_service.rag.evidence import Card
from rag_service.rag.generate import generate
from rag_service.rag.retrieve import retrieve

class RagState(TypedDict, total=False):
    set_num: str
    question: str
    history: list[dict]
    injected_cards: list[Card]
    collect: "CollectResult | None"
    cards: list[Card]
    answer: str
    used_cards: list[Card]
    no_citation: bool
    warnings: list[str]
    usage: dict
    timings: dict


@dataclass
class RagDeps:
    """의존성 생성은 실행 시에만 하므로 그래프 출력에는 키·DB가 필요 없다."""

    settings: dict = field(default_factory=load_settings)
    model: Any = None
    meta_store: Any = None
    docs_store: Any = None
    collect_fn: Callable[..., "CollectResult"] | None = None


@contextmanager
def default_deps() -> Iterator[RagDeps]:
    """기존 팩토리를 조합하고 이 호출이 소유한 연결만 정리한다."""
    from rag_service.llm import get_chat_model, get_embeddings

    settings = load_settings()
    model = get_chat_model(settings)
    inner = get_embeddings(settings, chunk_size=settings["index"]["embed_batch_size"],
                           check_embedding_ctx_length=False)
    path = resolve_path(settings["paths"]["sqlite"])
    if not path.is_file():
        raise ValueError("세트 DB가 없습니다. 데이터 적재를 먼저 실행하세요.")

    from rag_service.data_prep.collectors.http import HttpClient, build_user_agent
    from rag_service.data_prep.collectors.wikipedia import WikipediaCollector
    from rag_service.data_prep.doc_store import DocStore
    from rag_service.rag.collect import collect_for_question
    from rag_service.vectorstore.embed_cache import CachedEmbeddings
    from rag_service.vectorstore.store import get_collection, get_docs_collection

    contact = os.environ.get("WIKI_CONTACT", "").strip() or None
    api, app = settings["external_api"], settings["app"]
    interval = api["request_interval_sec"] if contact else api["interval_without_contact_sec"]
    client = HttpClient(build_user_agent(app["name"], app["version"], contact), interval,
                        api["max_retries"], api["timeout_sec"], api["backoff_base_sec"])
    conn = None
    try:
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        doc_store = DocStore(resolve_path(settings["paths"]["docs_dir"]),
                             resolve_path(settings["paths"]["collect_sqlite"]))
        embeddings = CachedEmbeddings(inner, resolve_path(settings["paths"]["embed_cache"]),
                                      settings["llm"]["embedding_model"], settings["index"]["embed_batch_size"])
        meta = get_collection(settings["collections"]["items"], embeddings, settings)
        docs = get_docs_collection(embeddings, settings)
        collect = partial(collect_for_question, settings=settings, conn=conn, model=model,
                          collector=WikipediaCollector(client, settings), doc_store=doc_store,
                          docs_store=docs, embeddings=embeddings)
        yield RagDeps(settings, model, meta, docs, collect)
    finally:
        if conn is not None:
            conn.close()
        client.session.close()


def build_graph(deps: RagDeps) -> CompiledStateGraph:
    """업무 노드는 정확히 retrieve와 generate 두 개다."""
    def retrieve_node(state: RagState, config: RunnableConfig) -> dict:
        return retrieve(state, deps, config.get("configurable", {}).get("on_event"))

    def generate_node(state: RagState, config: RunnableConfig) -> dict:
        return generate(state, deps, config.get("configurable", {}).get("on_event"))

    graph = StateGraph(RagState)
    graph.add_node("retrieve", retrieve_node)
    graph.add_node("generate", generate_node)
    graph.add_edge(START, "retrieve")
    graph.add_edge("retrieve", "generate")
    graph.add_edge("generate", END)
    return graph.compile()


def ask(set_num: str, question: str, history: list[dict] | None = None,
        injected_cards: list[Card] | None = None,
        on_event: Callable[[str, dict], None] | None = None,
        deps: RagDeps | None = None) -> RagState:
    """UI·CLI가 공유할 동기 질문 API. 전달받은 의존성은 닫지 않는다."""
    if not set_num.strip() or not question.strip():
        raise ValueError("세트 번호와 질문을 입력하세요.")
    if deps is None:
        with default_deps() as owned:
            return ask(set_num, question, history, injected_cards, on_event, owned)
    state: RagState = {"set_num": set_num, "question": question, "history": list(history or []),
                       "injected_cards": list(injected_cards or []), "warnings": [],
                       "usage": {}, "timings": {}}
    return build_graph(deps).invoke(state, config={"configurable": {"on_event": on_event}})
