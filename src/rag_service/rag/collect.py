"""질문 시점 P1 → 검색 → P2 → 수집·색인. 외부 의존성은 호출자가 주입한다."""

import hashlib
import json
import re
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from rag_service.data_prep.collectors.base import Candidate, Doc
from rag_service.data_prep.doc_store import DocStore, ensure_doc
from rag_service.llm import MissingApiKey
from rag_service.rag.prompts import p1_messages, p2_messages
from rag_service.rag.schemas import CandidateSelection, SearchQuery, Selected, p1_schema
from rag_service.rag.structured import invoke_structured
from rag_service.vectorstore.doc_index import DocWithScope, doc_sets, question_documents, sync_set_docs


@dataclass
class CollectResult:
    set_num: str
    queries: list[SearchQuery] = field(default_factory=list)
    en_terms: list[str] = field(default_factory=list)
    searched: list[str] = field(default_factory=list)
    skipped_queries: list[str] = field(default_factory=list)
    candidates: int = 0
    selected: list[tuple[str, str]] = field(default_factory=list)
    fetched_doc_ids: list[str] = field(default_factory=list)
    indexed_chunks: int = 0
    partial: bool = False
    elapsed_ms: int = 0
    llm_usage: dict = field(default_factory=dict)
    selected_titles: dict[str, str] = field(default_factory=dict)
    rejected: list[tuple[str, str, str]] = field(default_factory=list)


def _fallback_candidate(candidate: Candidate, question: str) -> bool:
    """P2 실패 시 시리즈와 매체·인물 문서를 보수적으로 제외한다."""
    text = f"{candidate.title} {candidate.snippet}".casefold()
    if candidate.title.casefold() in {"star wars", "lego star wars", "레고 스타워즈"}:
        return False
    categories = (
        (("video game", "videogame"), ("game", "게임")),
        (("novel",), ("novel", "소설")),
        (("comic",), ("comic", "코믹", "만화")),
        (("actor", "actress"), ("actor", "actress", "배우")),
        (("television series", "film series"), ("series", "시리즈", "작품")),
    )
    return all(not any(word in text for word in markers)
               or any(word in question.casefold() for word in asked)
               for markers, asked in categories)


class _Collection:
    def __init__(self, set_num: str, question: str, settings: dict, conn: sqlite3.Connection,
                 model: Any, collector: Any, doc_store: DocStore, docs_store: Any,
                 embeddings: Any, on_event: Callable[[str, dict], None] | None):
        self.result = CollectResult(set_num)
        self.question, self.settings, self.conn = question, settings, conn
        self.model, self.collector = model, collector
        self.doc_store, self.docs_store, self.embeddings = doc_store, docs_store, embeddings
        self.on_event = on_event
        self.cfg = settings["collection"]
        self.started = time.monotonic()
        self.pending: dict[str, DocWithScope] = {}
        self.p1_cached = False
        self.search_results: dict[str, set[str]] = {}
        self.empty_searches: set[str] = set()
        self.selection_done = False
        self.collected: set[str] = set()

    def event(self, name: str, **data: Any) -> None:
        if self.on_event:
            self.on_event(name, {"elapsed_ms": round((time.monotonic() - self.started) * 1000), **data})

    def within_budget(self) -> bool:
        if time.monotonic() - self.started >= self.cfg["question_time_budget_sec"]:
            self.result.partial = True
            return False
        return True

    def invoke(self, stage: str, messages: list, schema: type) -> Any:
        llm = self.settings["llm"]
        # 호출 사이에 예산을 확인할 수 있도록 재시도는 이 계층에서 수행한다.
        for _ in range(llm["max_retries"] + 1):
            if not self.within_budget():
                return None
            response = invoke_structured(self.model, messages, schema, llm["structured_method"], 0)
            usage = self.result.llm_usage.setdefault(stage, {"calls": 0})
            usage["calls"] += response.attempts
            for key, value in response.usage.items():
                usage[key] = usage.get(key, 0) + value
            if response.ok:
                return response.value
        self.result.partial = True
        return None

    def extract(self, set_name: str) -> None:
        self.event("extracting_queries")
        version = self.cfg["p1_prompt_version"]
        schema = p1_schema(version)
        messages = p1_messages(set_name, self.result.set_num, self.question, [], version)
        payload = json.dumps([message.content for message in messages], ensure_ascii=False)
        key = hashlib.sha256((version + payload).encode()).hexdigest()
        enabled = self.settings["llm"].get("cache_enabled", True)
        cached = self.doc_store.get_llm_cache(key, version) if enabled else None
        parsed = None
        if cached:
            try:
                parsed = schema.model_validate_json(cached)
                self.p1_cached = True
                self.result.llm_usage["p1"] = {"calls": 0, "cache_hit": True}
            except ValueError:
                pass
        if parsed is None:
            parsed = self.invoke("p1", messages, schema)
        if parsed is None or not parsed.queries or not parsed.queries[0].q.strip():
            parsed = schema.model_validate({"queries": [{"q": set_name, "lang": "en",
                                                         "purpose": "선택 세트 자체 문서"}]})
        if not self.p1_cached and enabled:
            self.doc_store.save_llm_cache(key, version, parsed.model_dump_json())
        queries = []
        for query in parsed.queries:
            q = query.q.strip()
            if version in {"p1-v2", "p1-v2.1"}:
                q = re.sub(r'\bLEGO\b|\b\d{5}(?:-\d+)?\b|["“”]', "", q, flags=re.IGNORECASE)
                q = " ".join(q.split())
            if q and (q, query.lang) not in {(row.q, row.lang) for row in queries}:
                queries.append(query.model_copy(update={"q": q}))
        self.result.queries = queries[:self.cfg["max_queries"]] or parsed.queries[:1]
        self.result.en_terms = parsed.en_terms

    def link(self, doc: Doc, title: str, query: str, reason: str) -> None:
        self.doc_store.link_question_doc(self.result.set_num, doc.doc_id, title, doc.lang, query, reason)
        kind = "list" if title.startswith("List of") or doc.title.startswith("List of") else "question"
        self.pending[doc.doc_id] = DocWithScope(doc, "entity", title, kind)

    def search(self) -> dict[str, tuple[Candidate, str]]:
        candidates = {}
        indexed = {meta["doc_id"] for meta in self.docs_store.get(include=["metadatas"])["metadatas"]}
        for query in self.result.queries:
            if not self.within_budget():
                break
            if self.doc_store.queried("wikipedia-search", query.q):
                self.result.skipped_queries.append(query.q)
                continue
            self.event("searching", q=query.q, lang=query.lang)
            try:
                found = self.collector.search(query.q, query.lang, self.cfg["candidates_per_query"])
                self.result.searched.append(query.q)
                query_ids = self.search_results.setdefault(query.q, set())
                if not found:
                    self.empty_searches.add(query.q)
                for candidate in found:
                    ident = f"wp-{candidate.lang}:{candidate.page_id}"
                    query_ids.add(ident)
                    if ident in indexed:
                        continue
                    reason = self.rejection_reason(candidate)
                    if reason:
                        if ident not in candidates:
                            self.reject(ident, candidate.title, reason)
                        continue
                    self.result.rejected = [row for row in self.result.rejected if row[0] != ident]
                    candidates.setdefault(ident, (candidate, query.q))
            except MissingApiKey:
                raise
            except Exception:
                self.doc_store.log_fetch("wikipedia-search", query.q, None, None, "error")
                self.result.partial = True
        self.result.candidates = len(candidates)
        return candidates

    def kind_hint(self, candidate: Candidate) -> str:
        text = f"{candidate.title} {candidate.snippet}".casefold()
        return ", ".join(kind for kind in ("video game", "novel", "comic", "board game", "card game")
                         if kind in text)

    def reject(self, ident: str, title: str, reason: str) -> None:
        """여러 검색어에 등장한 같은 후보는 한 번만 기록한다."""
        self.result.rejected = [row for row in self.result.rejected if row[0] != ident]
        self.result.rejected.append((ident, title, reason))

    def rejection_reason(self, candidate: Candidate) -> str | None:
        """P2와 실패 시 대체 선택에 같은 결정적 필터를 적용한다."""
        text = f"{candidate.title} {candidate.snippet}".casefold()
        if not any(keyword.casefold() in text for keyword in self.cfg["domain_keywords"]):
            return "도메인 단서 없음"
        if self.kind_hint(candidate) and not any(
            word in self.question.casefold() for word in ("게임", "소설", "코믹", "만화", "game", "novel", "comic")):
            return "질문에서 요청하지 않은 매체"
        return None

    def domain_document(self, doc: Doc) -> bool:
        """검색 스니펫 대신 원문의 제목과 도입부로 도메인을 재확인한다."""
        text = f"{doc.title}\n{doc.text()[:self.cfg['domain_intro_chars']]}".casefold()
        return any(word.casefold() in text for word in self.cfg["domain_keywords"])

    def select(self, set_name: str, candidates: dict) -> list[Selected]:
        if not candidates:
            self.selection_done = True
            if self.result.searched:
                self.selected_event()
            return []
        if not self.within_budget():
            return []
        self.event("selecting")
        rows = [{"id": ident, "title": candidate.title, "snippet": candidate.snippet,
                 "lang": candidate.lang, "kind_hint": self.kind_hint(candidate)}
                for ident, (candidate, _) in candidates.items()]
        parsed = self.invoke("p2", p2_messages(set_name, self.question, rows,
                                              self.cfg["max_docs_per_question"]), CandidateSelection)
        if parsed is None:
            choices = [Selected(id=ident, reason="선별 실패 시 안전한 첫 후보")
                       for ident, (candidate, _) in candidates.items()
                       if _fallback_candidate(candidate, self.question)][:1]
        else:
            choices = parsed.selected
        valid = {}
        for choice in choices:
            if choice.id in candidates:
                valid.setdefault(choice.id, choice)
        selected = list(valid.values())[:self.cfg["max_docs_per_question"]]
        self.result.selected = [(choice.id, choice.reason) for choice in selected]
        self.result.selected_titles = {choice.id: candidates[choice.id][0].title for choice in selected}
        for ident, (candidate, _) in candidates.items():
            if ident not in self.result.selected_titles:
                reason = "문서 수 상한 초과" if ident in valid else "선별되지 않음"
                self.reject(ident, candidate.title, reason)
        self.selected_event()
        self.selection_done = parsed is not None or bool(selected)
        return selected

    def selected_event(self) -> None:
        self.event("selected", selected=list(self.result.selected_titles.values()),
                   rejected_count=len(self.result.rejected))

    def complete_searches(self, choices: list[Selected]) -> None:
        """공유 후보를 포함해 해당 검색어의 선택 원문이 모두 저장된 뒤 완료한다."""
        if not self.selection_done:
            return
        selected = {choice.id for choice in choices}
        for query, ids in self.search_results.items():
            if (ids & selected) <= self.collected:
                self.doc_store.log_fetch("wikipedia-search", query, None, None,
                                         "missing" if query in self.empty_searches else "fetched")

    def fetch_one(self, lang: str, title: str, query: str, reason: str,
                  entity: str | None = None) -> Doc | None:
        if not self.within_budget():
            return None
        self.event("fetching", title=title, lang=lang)
        doc, _ = ensure_doc(self.doc_store, self.collector, lang, title, query)
        if doc is None:
            self.result.partial = True
            return None
        if not self.domain_document(doc):
            # 거절한 원문은 검색 완료로 기록하되 세트 연결·색인에는 넣지 않는다.
            self.collected.add(doc.doc_id)
            self.reject(doc.doc_id, doc.title, "도메인 단서 없음")
            self.event("skipped", reason="outside_domain", title=doc.title)
            return None
        self.link(doc, entity or doc.title, query, reason)
        if doc.doc_id not in self.result.fetched_doc_ids:
            self.result.fetched_doc_ids.append(doc.doc_id)
        return doc

    def fetch(self, choices: list[Selected], candidates: dict) -> None:
        originals = []
        for choice in choices:
            if not self.within_budget():
                break
            candidate, query = candidates[choice.id]
            try:
                doc = self.fetch_one(candidate.lang, candidate.title, query, choice.reason)
                if doc is not None:
                    self.collected.add(choice.id)
                    originals.append((doc, query, choice.reason))
            except MissingApiKey:
                raise
            except Exception:
                self.result.partial = True
        # 선택 원문을 먼저 확보하고 번역은 남은 시간 예산으로 추가한다.
        for doc, query, reason in originals:
            if doc.lang != "en" or not self.cfg["question_include_korean"]:
                continue
            if not self.within_budget():
                break
            try:
                found, korean = self.doc_store.get_language_link(doc, "ko")
                if not found:
                    korean = self.collector.langlinks([doc.title], "en", "ko")[doc.title]
                    self.doc_store.save_language_link(doc, "ko", korean)
                if korean:
                    self.fetch_one("ko", korean, query, reason, doc.title)
            except MissingApiKey:
                raise
            except Exception:
                self.result.partial = True

    def index(self) -> None:
        if not self.pending or not self.within_budget():
            return
        self.event("indexing")
        sets = doc_sets(self.conn, self.doc_store)
        linked = question_documents(self.doc_store, list(self.pending.values()))
        for doc_id in self.pending:
            if not self.within_budget():
                break
            try:
                docs = [item for item in linked if item.doc.doc_id == doc_id]
                sync_set_docs(self.docs_store, docs, sets, self.settings["chunking"],
                              prune=False, embeddings=self.embeddings)
                self.result.indexed_chunks += len(self.docs_store.get(where={"doc_id": doc_id}, include=[])["ids"])
            except MissingApiKey:
                raise
            except Exception:
                self.result.partial = True

    def run(self) -> CollectResult:
        try:
            row = self.conn.execute("SELECT name FROM sets WHERE set_num=?", (self.result.set_num,)).fetchone()
            if row is None:
                raise ValueError("선택 세트가 DB에 없습니다.")
            self.extract(row[0])
            candidates = self.search()
            choices = self.select(row[0], candidates)
            self.fetch(choices, candidates)
            self.complete_searches(choices)
            self.index()
            self.within_budget()
        except MissingApiKey:
            raise
        except Exception:
            self.result.partial = True
        self.result.elapsed_ms = round((time.monotonic() - self.started) * 1000)
        cached = (not self.result.partial and not self.result.searched
                  and not self.result.fetched_doc_ids and not self.pending
                  and len(self.result.skipped_queries) == len(self.result.queries))
        self.event("skipped" if cached else "done", **({"reason": "cached"} if cached else {}),
                   partial=self.result.partial)
        return self.result


def collect_for_question(set_num: str, question: str, *, settings: dict, conn: sqlite3.Connection,
                         model: Any, collector: Any, doc_store: DocStore, docs_store: Any,
                         embeddings: Any,
                         on_event: Callable[[str, dict], None] | None = None) -> CollectResult:
    """단계 경계에서 시간 예산을 확인하며 실패한 외부 응답은 UI에 노출하지 않는다."""
    return _Collection(set_num, question, settings, conn, model, collector, doc_store,
                       docs_store, embeddings, on_event).run()
