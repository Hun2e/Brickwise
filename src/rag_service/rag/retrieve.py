"""질문 수집 후 메타·문서·주입 근거를 결정적으로 병합한다."""

from collections import Counter
from dataclasses import replace
from time import perf_counter
from typing import TYPE_CHECKING, Any, Callable

from langchain_core.documents import Document

from rag_service.llm import MissingApiKey
from rag_service.rag.evidence import Card, text_hash

if TYPE_CHECKING:
    from rag_service.rag.graph import RagDeps, RagState


def mmr_caps(cfg: dict) -> str:
    """이전 boolean 설정도 유지하면서 선택적 상한을 해석한다."""
    mode = cfg.get("mmr_caps", "none")
    if mode not in {"none", "series_list_only", "all"}:
        raise ValueError("MMR 상한은 none, series_list_only 또는 all이어야 합니다.")
    return "all" if mode == "none" and cfg.get("mmr_apply_caps", False) else mode


def _meta_card(store: Any, set_num: str) -> Card | None:
    result = store.get(ids=[set_num], include=["documents", "metadatas"])
    if not result["ids"]:
        return None
    meta = result["metadatas"][0] or {}
    return Card(0, "set_meta", meta.get("name", meta.get("title", set_num)),
                f"https://rebrickable.com/sets/{set_num}/", "Rebrickable",
                result["documents"][0], item_id=set_num, doc_id=set_num,
                chunk_id=set_num, lang=meta.get("lang", "ko"))


def _doc_card(doc: Any, score: float, set_num: str) -> Card:
    meta = doc.metadata
    return Card(0, meta["source_type"], meta["title"], meta["url"], meta["license"],
                doc.page_content, item_id=set_num, doc_id=meta["doc_id"],
                chunk_id=doc.id or meta.get("chunk_id", ""), score=float(score),
                lang=meta.get("lang", ""), entity_kind=meta.get("entity_kind", ""),
                section_path=meta.get("section_path", ""))


def _merge(injected: list[Card], meta: Card | None, candidates: list,
           set_num: str, cfg: dict) -> list[Card]:
    cards: list[Card] = []
    ids: set[str] = set()
    hashes: set[str] = set()

    def append(card: Card) -> bool:
        digest = text_hash(card.text)
        if (card.chunk_id and card.chunk_id in ids) or digest in hashes:
            return False
        cards.append(replace(card, n=len(cards) + 1))
        if card.chunk_id:
            ids.add(card.chunk_id)
        hashes.add(digest)
        return True

    for card in injected:
        # 필수 메타와 겹치는 주입 카드보다 ID로 읽은 메타 원본을 보존한다.
        if meta and ((card.chunk_id and card.chunk_id == meta.chunk_id)
                     or text_hash(card.text) == text_hash(meta.text)):
            continue
        append(card)
    if meta:
        append(meta)
    counts: Counter[str] = Counter()
    docs: Counter[str] = Counter()
    cap_mode = "all" if cfg["search_type"] != "mmr" else mmr_caps(cfg)
    selected = 0
    for doc, score in candidates:
        if selected >= cfg["top_k"]:
            break
        card = _doc_card(doc, score, set_num)
        categories = (["series"] if doc.metadata.get("scope") == "series" else [])
        if card.entity_kind in {"group", "list"}:
            categories.append(card.entity_kind)
        capped_categories = categories if cap_mode == "all" else []
        if cap_mode == "series_list_only":
            capped_categories = [kind for kind in categories if kind in {"series", "list"}]
        if ((cap_mode == "all" and docs[card.doc_id] >= cfg["max_chunks_per_doc"])
                or any(counts[kind] >= cfg[f"max_{kind}_chunks"] for kind in capped_categories)):
            continue
        if append(card):
            docs[card.doc_id] += 1
            counts.update(categories)
            selected += 1
    return cards


def search_queries(question: str, cfg: dict, en_terms: list[str] | None,
                   meta: Card | None) -> list[str]:
    """실험과 서비스에서 동일한 질의 문자열을 사용한다."""
    terms = [term.strip() for term in (en_terms or []) if term.strip()][:cfg["max_en_terms"]]
    expansion = " ".join(terms) if terms else (meta.title if meta else "")
    expanded = " ".join(part for part in (question, expansion) if part)
    if cfg["query_mode"] == "question":
        return [question]
    if cfg["query_mode"] == "expanded":
        return [expanded]
    if cfg["query_mode"] == "dual":
        return list(dict.fromkeys([question, expanded]))
    raise ValueError("지원하지 않는 질의 구성입니다.")


def _union_hits(groups: list[list]) -> list:
    merged = {}
    for group in groups:
        for doc, score in group:
            key = doc.id or doc.metadata["chunk_id"]
            if key not in merged or score < merged[key][1]:
                merged[key] = (doc, score)
    return sorted(merged.values(), key=lambda hit: hit[1])


def _vectors(store: Any, docs: list) -> dict:
    if not docs:
        return {}
    data = store.get(ids=[doc.id or doc.metadata["chunk_id"] for doc in docs], include=["embeddings"])
    return dict(zip(data["ids"], data["embeddings"]))


def _distance(store: Any, query: list[float], vector: list[float]) -> float:
    """Chroma 거리와 같은 단위로 계산한다(L2는 제곱 거리)."""
    import numpy as np

    config = store._collection.configuration
    index = config.get("hnsw") or config.get("spann") or {}
    space = index.get("space", (store._collection.metadata or {}).get("hnsw:space", "l2"))
    a, b = np.asarray(query, dtype=np.float32), np.asarray(vector, dtype=np.float32)
    if space == "l2":
        return float(np.sum((a - b) ** 2))
    if space == "ip":
        return float(1 - np.dot(a, b))
    if space == "cosine":
        norm = np.linalg.norm(a) * np.linalg.norm(b)
        return float(1 - np.dot(a, b) / norm) if norm else 1.0
    raise ValueError("지원하지 않는 Chroma 거리입니다.")


def _mmr_hits(store: Any, queries: list[str], cfg: dict, where: dict) -> list:
    import numpy as np
    from langchain_chroma.vectorstores import maximal_marginal_relevance

    query_vectors = [store.embeddings.embed_query(query) for query in queries]
    if len(queries) == 1:
        docs = store.max_marginal_relevance_search_by_vector(
            query_vectors[0], k=cfg["top_k"], fetch_k=cfg["mmr_fetch_k"],
            lambda_mult=cfg["mmr_lambda"], filter=where)
        vectors = _vectors(store, docs)
        return [(doc, _distance(store, query_vectors[0], vectors[doc.id or doc.metadata["chunk_id"]]))
                for doc in docs]
    candidates = _union_hits([
        store.similarity_search_by_vector_with_relevance_scores(
            vector, k=cfg["mmr_fetch_k"], filter=where) for vector in query_vectors])
    vectors = _vectors(store, [doc for doc, _ in candidates])
    # dual은 두 질의의 후보를 합친 뒤 원래 질문을 기준으로 다양성을 선택한다.
    selected = maximal_marginal_relevance(
        np.asarray(query_vectors[0], dtype=np.float32),
        [vectors[doc.id or doc.metadata["chunk_id"]] for doc, _ in candidates],
        k=cfg["top_k"], lambda_mult=cfg["mmr_lambda"])
    # Chroma MMR과 동일하게 선택된 집합을 후보 거리 순서로 반환한다.
    return [hit for index, hit in enumerate(candidates) if index in selected]


def rank_docs(question: str, set_num: str, *, docs_store: Any, settings: dict,
              en_terms: list[str] | None = None, meta: Card | None = None,
              injected_cards: list[Card] | None = None,
              include_content: bool = False) -> list[tuple[dict, float]]:
    """수집 없이 외부 청크를 선택한다. 서비스는 본문을 함께 받아 재조회를 피한다."""
    cfg = settings["retrieval"]
    queries = search_queries(question, cfg, en_terms, meta)
    where = {"$or": [{"sets": {"$contains": set_num}}, {"scope": "series"}]}
    if not cfg["top_k"]:
        candidates = []
    elif cfg["search_type"] == "mmr":
        candidates = _mmr_hits(docs_store, queries, cfg, where)
    elif cfg["search_type"] == "similarity":
        groups = [docs_store.similarity_search_with_score(
            query, k=cfg["similarity_fetch_k"], filter=where) for query in queries]
        # 기본 경로는 기존 거리 동점 순서와 중복 처리 순서를 그대로 보존한다.
        candidates = sorted(groups[0], key=lambda hit: hit[1]) if len(groups) == 1 else _union_hits(groups)
    else:
        raise ValueError("지원하지 않는 검색 방식입니다.")
    prefix = _merge(injected_cards or [], meta, [], set_num, cfg)
    cards = _merge(injected_cards or [], meta, candidates, set_num, cfg)[len(prefix):]
    result = []
    for card in cards:
        doc = next(doc for doc, _ in candidates
                   if (doc.id or doc.metadata.get("chunk_id", "")) == card.chunk_id
                   and text_hash(doc.page_content) == text_hash(card.text))
        metadata = {**doc.metadata, "chunk_id": card.chunk_id}
        if include_content:
            metadata["page_content"] = doc.page_content
        result.append((metadata, card.score))
    return result


def retrieve(state: "RagState", deps: "RagDeps",
             on_event: Callable[[str, dict], None] | None = None) -> dict:
    """수집 실패는 기존 색인 검색을 막지 않으며 민감한 오류는 반환하지 않는다."""
    started = perf_counter()
    warnings = list(state.get("warnings", []))
    collected = None
    try:
        if deps.collect_fn is not None:
            collected = deps.collect_fn(state["set_num"], state["question"], on_event=on_event)
        if collected is not None and collected.partial:
            warnings.append("문서 수집이 일부만 완료되었습니다. 기존 색인도 함께 검색합니다.")
    except MissingApiKey:
        raise
    except Exception:
        warnings.append("문서 수집에 실패했습니다. 기존 색인을 사용하며 연결 상태를 확인하세요.")
    if on_event:
        on_event("retrieving", {})
    meta = _meta_card(deps.meta_store, state["set_num"])
    if meta is None:
        warnings.append("선택 세트의 메타 색인이 없습니다. index_items.py를 실행하세요.")
    cfg = deps.settings["retrieval"]
    ranked = rank_docs(state["question"], state["set_num"], docs_store=deps.docs_store,
                       settings=deps.settings, en_terms=collected.en_terms if collected else None,
                       meta=meta, injected_cards=state.get("injected_cards", []), include_content=True)
    candidates = [(Document(id=row["chunk_id"], page_content=row["page_content"],
                            metadata={k: v for k, v in row.items() if k != "page_content"}), score)
                  for row, score in ranked]
    cards = _merge(state.get("injected_cards", []), meta, candidates, state["set_num"], cfg)
    return {"collect": collected, "cards": cards, "warnings": warnings,
            "usage": {**state.get("usage", {}), **(collected.llm_usage if collected else {})},
            "timings": {**state.get("timings", {}),
                        "retrieve_ms": round((perf_counter() - started) * 1000)}}
