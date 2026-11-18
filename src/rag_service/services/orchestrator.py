"""그래프 밖에서 질의 분류·DB·검색·제작·RAG를 연결하는 공통 진입점."""

import hashlib
import json
import re
import sqlite3
import traceback
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from time import perf_counter, sleep
from typing import Any, Callable, Iterator

import httpx
from openai import APIConnectionError, APIStatusError, APITimeoutError, RateLimitError
from pydantic import BaseModel, ValidationError
from requests.exceptions import ConnectionError as RequestConnectionError, Timeout as RequestTimeout

from rag_service.config import load_settings, resolve_path
from rag_service.data_prep.collectors.http import UpstreamError
from rag_service.data_prep.doc_store import DocStore
from rag_service.db.queries import compare_versions
from rag_service.llm import MissingApiKey, get_chat_model
from rag_service.planner import NoInventory, SetNotFound, analyze, collection_have, normalize_set_num, recommend, set_parts
from rag_service.planner.collections import CollectionStore
from rag_service.planner.recommend import Recommendation
from rag_service.planner.sets import get_set
from rag_service.rag.citations import NO_CITATION_MESSAGE, filter_cited
from rag_service.rag.db_cards import recommend_card
from rag_service.rag.evidence import Card
from rag_service.rag.graph import RagDeps, ask
from rag_service.rag.prompts import p3_context, router_messages, selection_messages
from rag_service.rag.verify import verify_answer
from rag_service.rag.schemas import RouterOutput, RouterOutputV2, SelectionCriteria
from rag_service.rag.structured import invoke_structured
from rag_service.rag.templates import (
    TemplateAnswer, answer_build_analysis, answer_build_recommend, answer_compare,
    answer_fact, answer_set_ops, answer_recommendations, particle,
)
from rag_service.services.context_resolver import ResolvedContext
from rag_service.services.resolve import (
    Resolved, SetOutsideScope, contains_cue, explicit_set_numbers, explicit_set_tokens, resolve_sets,
)
from rag_service.services.search import search_sets

Event = Callable[[str, dict], None]


def log_exception(exc: Exception, settings: dict | None = None) -> None:
    """상세 오류는 파일에만 기록한다. 로그 쓰기 실패도 콘솔로 전파하지 않는다."""
    try:
        settings = settings if settings is not None else load_settings()
        path = resolve_path(settings["router"]["error_log"])
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(f"\n{datetime.now(timezone.utc).isoformat()}\n")
            stream.write("".join(traceback.format_exception(exc)))
    except Exception:
        pass


def _transient(exc: Exception) -> bool:
    if isinstance(exc, (APIConnectionError, APITimeoutError, RateLimitError, UpstreamError,
                        httpx.TransportError, RequestConnectionError, RequestTimeout,
                        ConnectionError, TimeoutError)):
        return True
    return isinstance(exc, APIStatusError) and (exc.status_code == 429 or 500 <= exc.status_code < 600)


def _failure(response: "OrchestratorResponse", exc: Exception, settings: dict | None = None) -> None:
    """실패한 경로의 중간 산출물·상세 오류를 사용자 응답에서 제거한다."""
    response.answer = ""
    response.cards = []
    response.tables = []
    response.warnings = []
    response.items = []
    response.state_delta = {}
    response.spec = None
    response.verification = {"checked": 0, "removed": 0, "items": []}
    if isinstance(exc, MissingApiKey):
        response.needs_input = "API 키가 설정되지 않았습니다. 실행 환경에 키를 설정하세요."
    elif _transient(exc):
        response.needs_input = "외부 서비스 응답이 일시적으로 불안정합니다. 잠시 후 다시 시도해 주세요"
        response.warnings = ["upstream_unavailable"]
    elif isinstance(exc, (SetNotFound, NoInventory)):
        response.needs_input = "세트 또는 부품 목록을 찾지 못했습니다. 세트 번호와 데이터 적재 상태를 확인하세요."
    elif isinstance(exc, (OSError, sqlite3.Error, ValueError, KeyError)):
        response.needs_input = "요청을 처리하지 못했습니다. 입력 형식과 DB·색인·설정을 확인하세요."
    else:
        response.needs_input = "처리 중 오류가 발생했습니다"
    if not isinstance(exc, MissingApiKey):
        log_exception(exc, settings)


def _notify(callback: Event | None, name: str, data: dict, settings: dict | None = None) -> None:
    if callback:
        try:
            callback(name, data)
        except Exception as exc:
            log_exception(exc, settings)


@dataclass
class OrchestratorResponse:
    route: str = "narrative"
    route_source: str = "fallback"
    answer: str = ""
    cards: list[Card] = field(default_factory=list)
    tables: list[dict] = field(default_factory=list)
    set_nums: list[str] = field(default_factory=list)
    needs_input: str | None = None
    warnings: list[str] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    timings: dict = field(default_factory=dict)
    verification: dict = field(default_factory=lambda: {"checked": 0, "removed": 0, "items": []})
    items: list[dict] = field(default_factory=list)
    state_delta: dict = field(default_factory=dict)
    spec: dict | None = None


@dataclass
class OrchestratorDeps:
    """테스트는 DB·LLM·검색·그래프를 주입한다. 주입한 자원은 닫지 않는다."""

    conn: sqlite3.Connection
    settings: dict = field(default_factory=load_settings)
    model: Any = None
    cache: DocStore | None = None
    collection_store: Any = None
    collection: dict | None = None
    meta_store: Any = None
    rag_deps: RagDeps | None = None
    ask_fn: Callable = ask
    search_fn: Callable = search_sets
    fact_fn: Callable = answer_fact
    set_ops_fn: Callable = answer_set_ops
    analysis_fn: Callable = answer_build_analysis
    build_fn: Callable = answer_build_recommend
    compare_fn: Callable = answer_compare
    recommend_fn: Callable = recommend


@contextmanager
def default_deps() -> Iterator[OrchestratorDeps]:
    """템플릿 경로에서는 벡터 저장소·임베딩을 생성하지 않는다."""
    settings = load_settings()
    path = resolve_path(settings["paths"]["sqlite"])
    if not path.is_file():
        raise ValueError("세트 DB가 없습니다.")
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        yield OrchestratorDeps(conn, settings)
    finally:
        conn.close()


def _model(deps: OrchestratorDeps) -> Any:
    if deps.model is None:
        deps.model = get_chat_model(deps.settings)
    return deps.model


def _cached(deps: OrchestratorDeps, messages: list, schema: type[BaseModel],
            version: str, response: OrchestratorResponse, stage: str) -> Any:
    if deps.cache is None:
        deps.cache = DocStore(resolve_path(deps.settings["paths"]["docs_dir"]),
                              resolve_path(deps.settings["paths"]["collect_sqlite"]))
    payload = {"messages": [(m.type, m.content) for m in messages],
               "model": deps.settings["llm"]["chat_model"], "schema": schema.model_json_schema()}
    key = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    enabled = deps.settings["llm"].get("cache_enabled", True)
    saved = deps.cache.get_llm_cache(key, version) if enabled else None
    if saved:
        try:
            value = schema.model_validate_json(saved)
            response.usage[stage] = {"cached": True}
            return value
        except ValidationError:
            pass
    cfg = deps.settings["llm"]
    result = invoke_structured(_model(deps), messages, schema, cfg["structured_method"], cfg["max_retries"])
    response.usage[stage] = {**result.usage, "cached": False, "attempts": result.attempts}
    if result.ok and enabled:
        deps.cache.save_llm_cache(key, version, result.value.model_dump_json())
    return result.value


def _collection(deps: OrchestratorDeps, cid: str | None) -> dict:
    if cid is None:
        return deps.collection or {}
    if deps.collection_store is None:
        deps.collection_store = CollectionStore(resolve_path(deps.settings["paths"]["user_sqlite"]))
    try:
        return deps.collection_store.get(cid)
    except KeyError:
        return {}


def _search_requested(out: RouterOutput, question: str, deps: OrchestratorDeps) -> bool:
    """특정 대상을 가리키지 않는 탐색 질문만 결정적으로 보정한다."""
    if out.route not in ("fact", "narrative") or out.set_nums or getattr(out, "set_refs", []):
        return False
    # 라우터가 번호를 누락해도 탐색으로 바꾸지 않고, 연도·수량은 제외한다.
    if explicit_set_tokens(question, deps.settings):
        return False
    references = load_settings(deps.settings["paths"]["dialogue_cues"])["references"]
    compact = re.sub(r"\s+", "", question)
    if any(re.sub(r"\s+", "", cue) in compact for cue in references):
        return False
    return any(contains_cue(question, cue) and (
        cue not in ("들어 있는", "들어있는")
        or any(companion in question for companion in ("찾아", "보여줘", "목록", "세트"))
    ) for cue in deps.settings["router"]["search_cues"])


def _rules(out: RouterOutput, question: str, selected: str | None,
           has_collection: bool, deps: OrchestratorDeps, source: str,
           context: ResolvedContext | None = None) -> tuple[str, str, Resolved]:
    cfg, route = deps.settings["router"], out.route
    from rag_service.data_prep.unsupported_attrs import unsupported_answer

    if unsupported_answer(question, deps.settings):
        return "fact", "rule:unsupported_attr", Resolved()
    try:
        numbers = explicit_set_numbers(question, deps.conn, out.set_nums, settings=deps.settings)
    except (SetNotFound, SetOutsideScope):
        return route, source, resolve_sets(route, out, question, selected, deps.conn, settings=deps.settings)
    out = out.model_copy(update={"set_nums": numbers})
    cues = load_settings(deps.settings["paths"]["dialogue_cues"])
    if (any(contains_cue(question, cue) for cue in cfg["recommend_followup_cues"])
            and (has_collection or context and context.summary.get("last_list_kind") == "recommend")
            and not explicit_set_tokens(question, deps.settings)
            and not any(contains_cue(question, cue) for cue in cues["refine"])):
        if not has_collection:
            return "build", "rule:no_collection", Resolved(
                needs_input="보유 세트 또는 부품을 컬렉션에 먼저 등록하세요.")
        # 재추천은 현재 대상의 부족 분석이 아니라 새 추천 목록을 요청한다.
        return "build", "rule:recommend_followup", Resolved()
    if (not numbers and any(contains_cue(question, cue) for cue in cues["refine"])):
        return "filter", "rule:list_refine", Resolved()
    if _search_requested(out, question, deps):
        route, source = "filter", "rule:search_cue"
    if (route == "build_archive"
            and any(contains_cue(question, cue) for cue in cfg["fact_cues"])
            and not any(contains_cue(question, cue) for cue in cfg["build_intent_cues"])):
        # 세트 속성과 세계관의 결합은 보유 부품을 분석하는 요청이 아니다.
        route, source = "narrative", "rule:attributes_with_story"
    recommendation_ref = (route == "build_archive"
                          and any(cue in question for cue in cfg["recommendation_cues"])
                          and any(cue in question for cue in cfg["recommendation_refs"]))
    resolved = resolve_sets(route, out, question, selected, deps.conn, settings=deps.settings)
    if route in ("build", "build_archive") and not has_collection:
        return route, "rule:no_collection", Resolved(needs_input="보유 세트 또는 부품을 컬렉션에 먼저 등록하세요.")
    if route == "fact":
        if any(cue in question for cue in cfg["compare_cues"]):
            comparison = resolve_sets("compare", out, question, selected, deps.conn, settings=deps.settings)
            if len(comparison.set_nums) >= 2 and not comparison.needs_input:
                route, source = "compare", "rule:compare_two_sets"
        if route == "fact" and not any(cue.casefold() in question.casefold() for cue in cfg["fact_cues"]):
            route, source = "narrative", "rule:fact_without_attribute"
    elif route == "narrative" and any(cue in question for cue in cfg["compare_cues"]):
        comparison = resolve_sets("compare", out, question, selected, deps.conn, settings=deps.settings)
        if comparison.set_nums and not comparison.needs_input:
            route, source = "compare", "rule:compare_cue"
    if source == "llm" or source == "fallback":
        if any(cue in question for cue in cfg["set_ops_cues"]):
            pair = resolve_sets("set_ops", out, question, selected, deps.conn, settings=deps.settings)
            if len(pair.set_nums) >= 2 and not pair.needs_input:
                route, source = "set_ops", "rule:two_sets_parts"
        if source == "llm" and out.confidence < cfg["min_confidence"]:
            route, source = "narrative", "rule:low_confidence"
    if route != out.route:
        resolved = resolve_sets(route, out, question, selected, deps.conn, settings=deps.settings)
    if route == "build_archive" and recommendation_ref:
        return route, "rule:recommendation_ref", Resolved()
    return route, source, resolved


def _template(response: OrchestratorResponse, result: TemplateAnswer) -> None:
    response.answer = result.answer
    response.cards = list(result.cards)
    response.tables.extend(result.tables)
    response.warnings.extend(result.warnings)
    response.items = list(result.items)


def _candidates(response: OrchestratorResponse, resolved: Resolved) -> None:
    response.needs_input = resolved.needs_input
    response.set_nums = resolved.set_nums
    if resolved.candidates:
        response.tables.append({"title": "세트 후보", "columns": ["세트 번호", "이름"],
                                "rows": [[r.set_num, r.name] for r in resolved.candidates], "formats": {}})


def select_recommendation(recs: list[Recommendation], criteria: SelectionCriteria) -> Recommendation | None:
    """부족 상한을 먼저 적용하고 동률은 세트 번호로 결정한다."""
    candidates = [r for r in recs if criteria.max_missing is None or r.total_missing <= criteria.max_missing]
    fields = {"year": lambda r: r.set_info.year, "num_parts": lambda r: r.set_info.num_parts,
              "missing": lambda r: r.total_missing, "coverage": lambda r: r.coverage}
    ordered = sorted(sorted(candidates, key=lambda r: r.set_info.set_num),
                     key=fields[criteria.sort], reverse=criteria.order == "desc")
    return ordered[0] if ordered else None


def _archive(question: str, collection: dict, deps: OrchestratorDeps,
             response: OrchestratorResponse, context: ResolvedContext | None = None) -> list[Card]:
    cfg, mode = deps.settings["router"], deps.settings["planner"]["default_mode"]
    if any(cue in question for cue in cfg["ignore_color_cues"]):
        mode = "ignore_color"
    if context and context.mode:
        mode = context.mode
    have = collection_have(deps.conn, collection.get("sets", []), collection.get("adjustments", []))
    owned = {normalize_set_num(deps.conn, number) for number, _ in collection.get("sets", [])}
    for number in owned:
        if not +set_parts(deps.conn, number):
            raise NoInventory(number)
    if not have:
        response.needs_input = "컬렉션이 비어 있습니다. 보유 세트 또는 부품을 먼저 등록하세요."
        return []
    recs = deps.recommend_fn(deps.conn, have, owned, mode=mode,
                             scope=deps.settings["planner"]["recommend_scope"], top_n=cfg["build_archive_pool"])
    refers_to_recommendation = response.route_source == "rule:recommendation_ref"
    criteria = (SelectionCriteria() if refers_to_recommendation else
                _cached(deps, selection_messages(question, cfg["selection_prompt_version"]),
                        SelectionCriteria, cfg["selection_prompt_version"], response, "selection"))
    if criteria is None:
        criteria = SelectionCriteria()
        response.warnings.append("선택 기준 추출에 실패하여 부족 수량이 가장 적은 세트를 선택했습니다. 조건을 확인하세요.")
    chosen = ((recs[0] if recs else None) if refers_to_recommendation
              else select_recommendation(recs, criteria))
    response.warnings.append(f"제작 추천 상위 {cfg['build_archive_pool']}개 후보 안에서 선택했습니다.")
    if chosen is None:
        response.answer = "선택 조건에 맞는 제작 추천 결과가 없습니다."
        response.set_nums = []
        return []
    response.set_nums = [chosen.set_info.set_num]
    labels = {("year", "asc"): "가장 오래된 세트", ("year", "desc"): "가장 최신 세트",
              ("missing", "asc"): "부족 수량이 가장 적은 세트", ("missing", "desc"): "부족 수량이 가장 많은 세트",
              ("coverage", "asc"): "커버리지가 가장 낮은 세트", ("coverage", "desc"): "커버리지가 가장 높은 세트",
              ("num_parts", "asc"): "부품 수가 가장 적은 세트", ("num_parts", "desc"): "부품 수가 가장 많은 세트"}
    condition = f"부족 {criteria.max_missing}개 이하 중 " if criteria.max_missing is not None else "추천 후보 중 "
    reason = ("제작 추천 순위가 가장 높은 세트" if refers_to_recommendation
              else condition + labels[(criteria.sort, criteria.order)])
    card = recommend_card([chosen], mode, 2)
    selection = replace(card, title="선택 이유", text=f"[DB] 선택 이유: {reason}\n{card.text}")
    if chosen.count_mismatch:
        response.warnings.append("선택 세트의 부품 수(Rebrickable 기준)와 부품 목록 집계가 다릅니다 (원본 불일치).")
    return [recommend_card(recs, mode, 1), selection]


def _search_store(deps: OrchestratorDeps) -> Any:
    if deps.meta_store is None and deps.search_fn is search_sets:
        from rag_service.llm import get_embeddings
        from rag_service.vectorstore.embed_cache import CachedEmbeddings
        from rag_service.vectorstore.store import get_collection

        settings = deps.settings
        inner = get_embeddings(settings, chunk_size=settings["index"]["embed_batch_size"],
                               check_embedding_ctx_length=False)
        embeddings = CachedEmbeddings(inner, resolve_path(settings["paths"]["embed_cache"]),
                                      settings["llm"]["embedding_model"], settings["index"]["embed_batch_size"])
        deps.meta_store = get_collection(settings["collections"]["items"], embeddings, settings)
    return deps.meta_store


def _items(conn: sqlite3.Connection, numbers: list[str]) -> list[dict]:
    return [{"rank": i, **{key: getattr(info, key) for key in ("set_num", "name", "year", "num_parts")}}
            for i, info in enumerate((get_set(conn, n) for n in numbers), 1)]


def _shown(response: OrchestratorResponse, kind: str, *, rec: dict | None = None) -> None:
    response.set_nums = [item["set_num"] for item in response.items]
    response.state_delta.update(last_list={"kind": kind, "turn_id": None,
        "items": list(response.set_nums), "spec": response.spec, "rec": rec}, pending=None)


def _recommend_params(deps: OrchestratorDeps, mode: str, context: ResolvedContext | None) -> dict:
    previous = context.recommend if context and context.recommend else {}
    return {"mode": mode, "top_n": previous.get("top_n", deps.settings["planner"]["recommend_top_n"]),
            "scope": previous.get("scope", ",".join(deps.settings["planner"]["recommend_scope"]))}


def _clarify(response: OrchestratorResponse, question: str, clarification: dict,
             deps: OrchestratorDeps, intent: str = "narrative") -> None:
    options = clarification["options"]
    response.needs_input = clarification["question"]
    response.items = _items(deps.conn, options)
    if options:
        _shown(response, "candidates")
    response.state_delta["pending"] = {"kind": clarification["kind"], "options": list(options),
                                       "request": {"intent": intent, "question": question}}


def _refined_recommendations(question: str, collection: dict, deps: OrchestratorDeps,
                             response: OrchestratorResponse, context: ResolvedContext,
                             event: Event) -> None:
    """표시했던 추천에만 조건을 적용하고 부족 수량은 최신 컬렉션으로 계산한다."""
    refine = context.refine
    mode = context.mode or deps.settings["planner"]["default_mode"]
    cues = load_settings(deps.settings["paths"]["dialogue_cues"])
    missing = re.search(cues["missing_pattern"], question)
    filter_question = re.sub(cues["missing_pattern"], "", question)
    result = deps.search_fn(filter_question, conn=deps.conn, model=_model(deps),
        meta_store=_search_store(deps), settings=deps.settings, on_event=event,
        base_spec=refine["base_spec"], allow=refine["allow"], keep_order=refine["allow"])
    have = collection_have(deps.conn, collection.get("sets", []), collection.get("adjustments", []))
    recs = []
    for number in result.set_nums:
        r = analyze(deps.conn, have, number, mode)
        if missing:
            value, op = int(missing[1].replace(",", "")), missing[2]
            if not {"이하": r.total_missing <= value, "미만": r.total_missing < value,
                    "이상": r.total_missing >= value, "초과": r.total_missing > value}[op]:
                continue
        recs.append(Recommendation(r.target, r.total_missing, r.lots_missing, r.coverage,
                                   r.need_total, r.official_num_parts != r.need_total))
    _template(response, answer_recommendations(recs, mode))
    response.spec = json.loads(json.dumps(asdict(result.spec)))
    response.usage["search"] = result.usage
    response.timings["search_ms"] = result.elapsed_ms
    response.warnings.extend(result.warnings)


def _refine_state(question: str, deps: OrchestratorDeps, response: OrchestratorResponse,
                  context: ResolvedContext) -> None:
    if not response.items:
        facts = [get_set(deps.conn, n) for n in context.refine["allow"]]
        years = ", ".join(str(y) for y in sorted({f.year for f in facts}))
        card = Card(n=len(response.cards) + 1, source_type="db", title="이전 목록의 출시 연도",
                    url="https://rebrickable.com/downloads/", license="Rebrickable",
                    text="\n".join(f"{f.set_num} {f.name}: 출시 연도 {f.year}" for f in facts))
        response.cards.append(card)
        response.answer = f"그중 조건에 맞는 세트는 없습니다. 목록의 출시 연도: {years}. [근거 {card.n}]"
        response.set_nums = []
        response.state_delta = {}
        return
    kind = "recommend" if context.refine["kind"] == "recommend" else "search"
    rec = None
    if kind == "recommend":
        mode = context.mode or deps.settings["planner"]["default_mode"]
        rec = {**(context.refine.get("rec") or _recommend_params(deps, mode, context)), "mode": mode}
        response.state_delta["mode"] = mode
    _shown(response, kind, rec=rec)
    if len(response.items) == 1:
        response.state_delta.update(subject={"set_num": response.set_nums[0], "source": "list", "turn_id": None},
                                    compare=[], pending=None)
        info = response.items[0]
        response.answer += f" {info['set_num']} {info['name']}{particle(info['name'], '을/를')} 현재 대상으로 선택했어요. [근거 1]"
    else:
        response.needs_input = response.answer + " 어느 세트로 이어갈까요?"
        response.state_delta["pending"] = {"kind": "target", "options": response.set_nums,
            "request": {"intent": "select", "question": question}}


def _dispatch(question: str, history: list[dict] | None, collection: dict,
              deps: OrchestratorDeps, response: OrchestratorResponse, event: Event,
              selected_set: str | None = None, context: ResolvedContext | None = None) -> None:
    route, numbers, mode = response.route, response.set_nums, deps.settings["planner"]["default_mode"]
    if response.route_source == "rule:unsupported_attr":
        from rag_service.data_prep.unsupported_attrs import unsupported_answer

        response.answer = unsupported_answer(question, deps.settings)
        return
    if any(cue in question for cue in deps.settings["router"]["ignore_color_cues"]):
        mode = "ignore_color"
    if context and context.mode:
        mode = context.mode
    if route == "filter":
        event("searching", {})
        refine = context.refine if context else None
        if refine and refine["kind"] == "recommend":
            _refined_recommendations(question, collection, deps, response, context, event)
            _refine_state(question, deps, response, context)
            return
        kwargs = ({"base_spec": refine["base_spec"], "allow": refine["allow"],
                   "keep_order": refine["allow"]} if refine else {})
        result = deps.search_fn(question, conn=deps.conn, model=_model(deps), meta_store=_search_store(deps),
                                settings=deps.settings, on_event=event, **kwargs)
        _template(response, result.template)
        response.set_nums = result.set_nums
        response.warnings.extend(result.warnings)
        response.usage["search"] = result.usage
        response.timings["search_ms"] = result.elapsed_ms
        response.items = _items(deps.conn, result.set_nums)
        response.spec = json.loads(json.dumps(asdict(result.spec))) if hasattr(result, "spec") else None
        if refine:
            _refine_state(question, deps, response, context)
        else:
            _shown(response, "search")
        return
    if route in ("fact", "set_ops", "build"):
        event("querying", {})
        if route == "fact":
            result = deps.fact_fn(deps.conn, numbers[0], question)
        elif route == "set_ops":
            if len(numbers) != 2:
                response.needs_input = "부품 비교는 서로 다른 두 세트를 지정해 주세요."
                return
            result = deps.set_ops_fn(deps.conn, *numbers, mode=mode)
        elif numbers:
            result = deps.analysis_fn(deps.conn, collection, numbers[0], mode)
        else:
            rec = _recommend_params(deps, mode, context)
            result = deps.build_fn(deps.conn, collection, mode,
                                   {"top_n": rec["top_n"], "scope": rec["scope"].split(",")} if context else {})
        _template(response, result)
        if route == "build":
            response.state_delta["mode"] = mode
            if not numbers:
                _shown(response, "recommend", rec=rec)
        return
    injected: list[Card] = []
    if route == "compare":
        event("querying", {})
        compared_numbers: set[str] = set()
        # 서로 다른 기체를 명시한 비교도 빠뜨리지 않고 같은 판본 묶음은 한 번만 주입한다.
        for number in numbers:
            result = deps.compare_fn(deps.conn, number)
            for table in result.tables:
                if table.get("title") == "판본 비교" and "세트 번호" in table.get("columns", []):
                    index = table["columns"].index("세트 번호")
                    for row in table["rows"]:
                        compared_numbers.update(str(row[index]).split("/"))
            if selected_set and selected_set not in compared_numbers and deps.compare_fn is answer_compare:
                # 표시 행 상한 때문에 표에 생략된 판본도 기준 세트가 될 수 있다.
                full = compare_versions(deps.conn, number, deps.settings)
                compared_numbers.update(n for row in full.rows for n in row["set_nums"])
            if result.cards and result.cards[0].text not in {c.text for c in injected}:
                start = len(injected)
                injected.extend(replace(c, n=start + i) for i, c in enumerate(result.cards, 1))
                response.tables.extend(result.tables)
            response.warnings.extend(result.warnings)
        if selected_set in compared_numbers:
            response.set_nums = [selected_set, *(n for n in numbers if n != selected_set)]
    elif route == "build_archive":
        event("querying", {})
        injected = _archive(question, collection, deps, response, context)
        if not injected:
            return
    selected_name = deps.conn.execute("SELECT name FROM sets WHERE set_num=?", (response.set_nums[0],)).fetchone()
    with p3_context(selected_set={"set_num": response.set_nums[0], "name": selected_name[0] if selected_name else ""},
                    table_titles=[table["title"] for table in response.tables], settings=deps.settings):
        rag_question = (context.rag_question if context and context.subject == response.set_nums[0]
                        and context.rag_question else question)
        result = deps.ask_fn(response.set_nums[0], rag_question, history=history,
                             injected_cards=injected, on_event=event, deps=deps.rag_deps)
    response.answer = result["answer"]
    response.cards = list(result.get("cards", result.get("used_cards", [])))
    if route == "build_archive":
        selection = next((c for c in response.cards if c.title == "선택 이유"
                          and c.text == injected[-1].text), None)
        if selection is not None:
            reason = selection.text.splitlines()[0].removeprefix("[DB] ")
            response.answer = f"{reason} [근거 {selection.n}]\n\n{response.answer}"
    response.warnings.extend(result.get("warnings", []))
    response.usage["rag"] = result.get("usage", {})
    response.timings.update(result.get("timings", {}))


def _verify_response(question: str, deps: OrchestratorDeps, response: OrchestratorResponse,
                     context_set_nums: list[str] | None = None) -> None:
    """템플릿은 검사 결과만 기록하고 P3 응답만 교정한다."""
    if response.needs_input or not response.answer or not deps.settings["verify"]["enabled"]:
        return
    numbers = list(dict.fromkeys([*(context_set_nums or []), *response.set_nums]))
    result = verify_answer(response.answer, response.cards, question, deps.conn,
                           context_set_nums=numbers, settings=deps.settings)
    generated = response.route in ("narrative", "compare", "build_archive")
    response.verification = {"checked": result.checked, "removed": len(result.removed) if generated else 0,
                             "items": result.items}
    if generated:
        response.answer = result.answer
        response.warnings.extend(result.warnings)
    elif result.removed:
        response.warnings.append("템플릿 수치 검증에 실패했습니다. DB 근거 생성 로직을 확인하세요.")
    try:
        path = resolve_path(deps.settings["verify"]["log_path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(), "route": response.route,
                                     **response.verification, "violations": result.removed}, ensure_ascii=False) + "\n")
    except OSError as exc:
        log_exception(exc, deps.settings)


def _dispatch_with_retry(question: str, history: list[dict] | None, collection: dict,
                         deps: OrchestratorDeps, response: OrchestratorResponse,
                         event: Event, selected_set: str | None,
                         context: ResolvedContext | None = None) -> OrchestratorResponse:
    """같은 경로를 최대 한 번 재시도하며 성공한 시도의 산출물만 반영한다."""
    for attempt in range(2):
        current = deepcopy(response)
        try:
            _dispatch(question, history, collection, deps, current, event, selected_set, context)
            return current
        except Exception as exc:
            if not _transient(exc) or attempt:
                raise
            log_exception(exc, deps.settings)
            sleep(deps.settings["router"]["retry_backoff_sec"])
    raise AssertionError("unreachable")


def answer(question: str, *, selected_set: str | None = None, history: list[dict] | None = None,
           collection_id: str | None = None, deps: OrchestratorDeps | None = None,
           on_event: Event | None = None, context: ResolvedContext | None = None) -> OrchestratorResponse:
    """모든 경로와 복구 가능한 오류를 UI 공통 응답으로 반환한다."""
    if deps is None:
        try:
            with default_deps() as owned:
                return answer(question, selected_set=selected_set, history=history,
                              collection_id=collection_id, deps=owned, on_event=on_event, context=context)
        except Exception as exc:
            response = OrchestratorResponse()
            _failure(response, exc)
            response.answer = response.needs_input
            _notify(on_event, "done", {"route": response.route, "needs_input": True})
            return response
    started = perf_counter()
    response = OrchestratorResponse()

    def event(name: str, data: dict) -> None:
        # 하위 서비스의 done을 최종 응답 완료로 오해하지 않게 한 번만 발행한다.
        if on_event and name != "done":
            _notify(on_event, name, data, deps.settings)

    try:
        event("routing", {})
        if not question.strip():
            response.needs_input = "질문을 입력하세요."
            return response
        if context:
            selected_set = context.subject
            response.state_delta = deepcopy(context.state_delta)
            if context.subject and context.subject_source in ("explicit", "list", "card", "ui"):
                response.state_delta.update(subject={"set_num": context.subject,
                    "source": context.subject_source, "turn_id": None}, compare=[], pending=None)
            response.warnings.extend(context.notes)
            if context.clarify:
                _clarify(response, question, context.clarify, deps)
                return response
            if context.resume:
                question = ("세트 정보 알려줘" if context.resume["intent"] == "select"
                            else context.resume["question"])
        collection = _collection(deps, collection_id)
        has_collection = bool(collection.get("sets") or collection.get("adjustments"))
        selected = None
        if selected_set:
            try:
                info = get_set(deps.conn, selected_set)
                selected = {"set_num": info.set_num, "name": info.name}
            except SetNotFound as exc:
                _candidates(response, Resolved(needs_input="선택 세트 번호를 확인하세요.", candidates=exc.candidates))
                event("resolving", {})
                return response
        cfg = deps.settings["router"]
        version = cfg["prompt_version"]
        messages = router_messages(question, has_collection, version, selected_set=selected, history=history,
                                    history_answer_chars=cfg["history_answer_chars"],
                                    context=context.summary if context else None)
        schema = RouterOutputV2 if version in {"router-v2", "router-v3", "router-v4"} else RouterOutput
        routed_at = perf_counter()
        out = _cached(deps, messages, schema, version, response, "router")
        response.timings["routing_ms"] = round((perf_counter() - routed_at) * 1000)
        if out is None:
            out = RouterOutputV2(route="narrative", confidence=0)
            response.warnings.append("질문 분류에 실패하여 근거 검색 경로로 처리합니다.")
            if not cfg["llm_fallback"]:
                response.needs_input = "질문을 분류하지 못했습니다. 세트와 요청을 구체적으로 입력하세요."
                event("resolving", {})
                return response
        else:
            response.route_source = "llm"
        if context:
            updates = {"set_nums": list(context.compare or ((context.subject,) if context.subject else ()))}
            if isinstance(out, RouterOutputV2):
                updates["set_refs"] = []
            if context.refine:
                updates.update(route="filter", confidence=1)
            elif context.mode_followup or context.recommend:
                updates.update(route="build", confidence=1)
            elif context.resume and context.resume["intent"] == "select":
                updates.update(route="fact", confidence=1)
                question = "세트 부품 수와 출시 연도 알려줘"
            out = out.model_copy(update=updates)
        event("resolving", {})
        response.route, response.route_source, resolved = _rules(
            out, question, selected_set, has_collection, deps, response.route_source, context)
        _candidates(response, resolved)
        if context and context.compare and not response.needs_input:
            response.state_delta["compare"] = list(context.compare)
        if response.needs_input and context and response.route_source == "rule:no_collection":
            response.state_delta.pop("mode", None)
        elif response.needs_input and context:
            _clarify(response, question, {"question": response.needs_input,
                "options": [r.set_num for r in resolved.candidates], "kind": "target"}, deps, response.route)
        if not response.needs_input:
            context_set_nums = [*([selected["set_num"]] if selected else []), *resolved.set_nums]
            response = _dispatch_with_retry(question, history, collection, deps, response, event,
                                             selected["set_num"] if selected else None, context)
            if not response.needs_input and response.answer and deps.settings["verify"]["enabled"]: event("verifying", {})
            _verify_response(question, deps, response, context_set_nums)
    except Exception as exc:
        _failure(response, exc, deps.settings)
    finally:
        if response.needs_input:
            response.answer = response.needs_input
        response.cards, no_citation, warnings = filter_cited(response.answer, response.cards)
        response.warnings.extend(warnings)
        if no_citation and not response.needs_input:
            response.warnings.append(NO_CITATION_MESSAGE)
        if response.needs_input:
            response.warnings = [w for w in response.warnings if w != NO_CITATION_MESSAGE]
        response.warnings = list(dict.fromkeys(response.warnings))
        response.timings["total_ms"] = round((perf_counter() - started) * 1000)
        _notify(on_event, "done", {"route": response.route, "needs_input": bool(response.needs_input)}, deps.settings)
    return response
