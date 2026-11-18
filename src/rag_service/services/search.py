"""조건 추출·DB 필터·의미 정렬·검수를 연결하는 조건 탐색 서비스."""

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from time import perf_counter
from typing import Any, TypeVar

from pydantic import BaseModel

from rag_service.config import load_settings, resolve_path
from rag_service.data_prep.doc_store import DocStore
from rag_service.data_prep.entities import load_rules
from rag_service.db.queries import FilterSpec, SetFact, filter_sets
from rag_service.rag.db_cards import filter_card
from rag_service.rag.prompts import filter_messages, review_messages
from rag_service.rag.schemas import Dropped, FilterQuery, ReviewResult
from rag_service.rag.structured import invoke_structured
from rag_service.rag.templates import TemplateAnswer

T = TypeVar("T", bound=BaseModel)


@dataclass
class SearchResult:
    query: FilterQuery | None
    spec: FilterSpec
    template: TemplateAnswer
    set_nums: list[str]
    no_result: bool
    discarded_conditions: list[str]
    total_sql: int
    reviewed: int
    dropped: list[Dropped]
    warnings: list[str]
    usage: dict
    elapsed_ms: int
    added_conditions: list[str] = field(default_factory=list)


def _token_forms(token: str) -> set[str]:
    """원형도 보존해 '타이'처럼 조사와 같은 글자로 끝나는 이름을 지킨다."""
    forms = {token.casefold()}
    for particle in ("에서", "으로", "가", "이", "는", "은", "를", "을", "의", "와", "과",
                     "로", "에", "도", "만"):
        if token.endswith(particle) and len(token) > len(particle):
            forms.add(token[:-len(particle)].casefold())
            break
    return forms


def _mentioned(term: str, question: str) -> bool:
    words = re.findall(r"[\w-]+", term.casefold())
    tokens = [_token_forms(token) for token in re.findall(r"[\w-]+", question)]
    if not words:
        return False
    return any(all(word in forms for word, forms in zip(words, tokens[start:]))
               for start in range(len(tokens) - len(words) + 1))


def _alias_components(alias: str) -> set[str]:
    """괄호 속 설명은 이름의 고유 구성 단어로 사용하지 않는다."""
    name = re.sub(r"\([^)]*\)|（[^）]*）", " ", alias)
    return {word.casefold() for word in name.split()
            if re.search(r"[가-힣]", word) and re.fullmatch(r"[\w-]+", word)}


def _aliases(conn: sqlite3.Connection, question: str) -> list[dict]:
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='ko_aliases'").fetchone():
        return []
    rows = conn.execute(
        "SELECT alias, canonical_en, kind FROM ko_aliases ORDER BY alias, canonical_en").fetchall()
    components = [_alias_components(alias) for alias, _, _ in rows]
    meanings: dict[str, set[str]] = {}
    for (_, canonical, _), words in zip(rows, components):
        for word in words:
            meanings.setdefault(word, set()).add(canonical.casefold())
    question_words = set().union(*(_token_forms(token) for token in re.findall(r"[\w-]+", question)))
    return [{"alias": alias, "canonical_en": canonical, "kind": kind}
            for (alias, canonical, kind), words in zip(rows, components)
            if _mentioned(alias, question)
            or any(len(meanings[word]) == 1 for word in words & question_words)]


def _terms(question: str, settings: dict) -> list[dict]:
    """PM 관리 용어 파일은 읽기만 하며 질문에 나온 항목만 전달한다."""
    terms = load_settings(settings["paths"]["ko_terms"])["terms"]
    return [{"alias": term, "canonical_en": english}
            for term, names in terms.items() if _mentioned(term, question)
            for english in names]


def _vehicle_keywords(question: str, aliases: list[dict], terms: list[dict], settings: dict) -> list[str]:
    """위키 제목 전체 대신 vehicle 패턴에 실제 매칭되는 세트명 표기를 쓴다."""
    rules, _ = load_rules(resolve_path(settings["paths"]["entity_rules"]))
    candidates: list[str] = []
    for rule in rules:
        if rule.kind != "vehicle":
            continue
        # 패턴의 부분 문자열 오인식을 막고 영어 뒤 한국어 조사도 허용한다.
        pattern = re.compile(r"(?<![A-Za-z0-9])(?:" + rule.pattern.pattern + r")(?![A-Za-z0-9])",
                             re.IGNORECASE | re.ASCII)
        explicit = [match.group() for match in pattern.finditer(question)]
        explicit.extend(row["canonical_en"] for row in terms
                        if row["alias"] in {"엑스윙", "타이", "에이티에이티"}
                        and pattern.fullmatch(row["canonical_en"]))
        if explicit:
            candidates.extend(explicit)
            continue
        for row in aliases:
            if (row["kind"] == "vehicle" and _mentioned(row["alias"], question)
                    and row["canonical_en"].casefold() == rule.wiki_title.casefold()):
                match = pattern.search(row["canonical_en"])
                if match:
                    candidates.append(match.group())
    return list({word.casefold(): word for word in candidates}.values())


def _add_vehicle_conditions(query: FilterQuery, keywords: list[str]) -> list[str]:
    """유효한 제외 조건을 존중하며 모델이 누락한 이름 조건을 보강한다."""
    added = []
    for word in keywords:
        if any(word.casefold() in existing.casefold() for existing in query.name_keywords):
            continue
        if any(existing.casefold() in word.casefold() or word.casefold() in existing.casefold()
               for existing in query.exclude_keywords):
            continue
        query.name_keywords.append(word)
        added.append(f"name_keywords={word}")
    return added


def _validate_conditions(query: FilterQuery, question: str, aliases: list[dict],
                         warnings: list[str], terms: list[dict],
                         settings: dict | None = None) -> tuple[FilterQuery, list[str]]:
    """원문·별칭으로 지지되지 않은 조건만 버리고 유효한 조건은 유지한다."""
    query = query.model_copy(deep=True)
    discarded = []
    labels = (settings if settings is not None else load_settings())["search"]["field_labels"]

    def discard(field: str, value: Any) -> None:
        discarded.append(f"{field}={value}")
        # 모델이 반환한 임의 텍스트를 사용자 경고에 그대로 노출하지 않는다.
        warnings.append(f"질문에서 확인되지 않은 {labels.get(field, '검색')} 조건을 제외했습니다.")

    full_aliases = [row for row in aliases if _mentioned(row["alias"], question)]
    unique_components = [row for row in aliases if not _mentioned(row["alias"], question)]

    def supported(word: str, *, include_terms: bool) -> bool:
        if not word:
            return False
        if word.casefold() in question.casefold():
            return True
        for mappings in (terms if include_terms else [], full_aliases, unique_components):
            if any(word.casefold() in row["canonical_en"].casefold() for row in mappings):
                return True
        return False

    for field in ("name_keywords", "exclude_keywords"):
        accepted = []
        for raw in getattr(query, field):
            word = raw.strip()
            if supported(word, include_terms=True):
                if word.casefold() not in [value.casefold() for value in accepted]:
                    accepted.append(word)
            else:
                discard(field, raw)
        setattr(query, field, accepted)
    if query.minifig is not None:
        query.minifig = query.minifig.strip()
        if not supported(query.minifig, include_terms=False):
            discard("minifig", query.minifig)
            query.minifig = None
    if query.has_minifig is not None and not any(word in question for word in ("미니피그", "피규어")):
        discard("has_minifig", query.has_minifig)
        query.has_minifig = None
    has_number = re.search(
        r"[0-9]|(?<![가-힣])(?:[일이삼사오육칠팔구십백천만억]*[십백천만억]"
        r"[일이삼사오육칠팔구십백천만억]*|한|두|세|네|다섯|여섯|일곱|여덟|아홉|열)"
        r"(?=\s|개|년|이상|이하|미만|초과|부터|까지|$)", question)
    if not has_number:
        for field in ("year_from", "year_to", "parts_min", "parts_max", "limit"):
            value = getattr(query, field)
            if value is not None:
                discard(field, value)
                setattr(query, field, None)
    if query.sort != "relevance" and not re.search(
        r"순|정렬|오름차|내림차|sort|order|newest|latest|oldest|highest|lowest", question, re.I
    ):
        discard("sort", query.sort)
        query.sort, query.order = "relevance", "asc"
    query.semantic = (query.semantic or "").strip() or None
    return query, discarded


def _cached_call(model: Any, messages: list, schema: type[T], version: str,
                 cache: DocStore, settings: dict, usage: dict, stage: str) -> T | None:
    payload = json.dumps({"version": version, "llm": {k: v for k, v in settings["llm"].items()
                                                     if k != "cache_enabled"},
                          "messages": [message.content for message in messages]},
                         sort_keys=True, ensure_ascii=False)
    key = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    enabled = settings["llm"].get("cache_enabled", True)
    cached = cache.get_llm_cache(key, version) if enabled else None
    if cached:
        try:
            parsed = schema.model_validate_json(cached)
            usage[stage] = {"calls": 0, "cache_hit": True}
            return parsed
        except ValueError:
            pass
    response = invoke_structured(model, messages, schema, settings["llm"]["structured_method"],
                                 settings["llm"]["max_retries"])
    usage[stage] = {"calls": response.attempts, "cache_hit": False, **response.usage}
    if response.value is not None and enabled:
        cache.save_llm_cache(key, version, response.value.model_dump_json())
    return response.value


def _rank(rows: list[SetFact], question: str, semantic: str, meta_store: Any,
          maximum: int, warnings: list[str]) -> list[SetFact]:
    by_id = {row.set_num: row for row in rows}
    hits = meta_store.similarity_search_with_score(
        f"{semantic} {question}", k=min(maximum, len(rows)),
        filter={"set_num": {"$in": list(by_id)}})
    ranked = []
    seen = set()
    for document, score in sorted(hits, key=lambda hit: hit[1]):
        number = document.metadata.get("set_num")
        if number in by_id and number not in seen:
            ranked.append(by_id[number])
            seen.add(number)
    if len(ranked) < min(maximum, len(rows)):
        warnings.append("일부 후보의 메타 색인이 없습니다. 항목 색인을 확인하세요.")
    return ranked[:maximum]


def _review(rows: list[SetFact], response: ReviewResult,
            warnings: list[str]) -> tuple[list[SetFact], list[Dropped]]:
    candidates = {row.set_num for row in rows}
    keep = set(response.keep) & candidates
    dropped = {}
    invalid = (set(response.keep) | {item.id for item in response.drop}) - candidates
    if invalid:
        warnings.append("검수 응답의 후보 밖 세트 번호를 무시했습니다.")
    for item in response.drop:
        # 모순되거나 누락된 판정은 '애매하면 남긴다' 규칙으로 처리한다.
        if item.id in candidates and item.id not in keep and item.reason.strip():
            dropped.setdefault(item.id, item)
    if keep & {item.id for item in response.drop}:
        warnings.append("검수 판정이 모순된 후보는 유지했습니다.")
    return ([row for row in rows if row.set_num not in dropped],
            [dropped[row.set_num] for row in rows if row.set_num in dropped])


def _template(rows: list[SetFact], total: int, spec: FilterSpec,
              warnings: list[str], final_sort: str) -> TemplateAnswer:
    card = filter_card(rows, total, spec, 1)
    text = card.text.replace(f"정렬: {spec.sort} {spec.order}",
                             f"SQL 정렬: {spec.sort} {spec.order}\n최종 정렬: {final_sort}")
    if spec.exclude_keywords:
        text += "\n제외 이름 키워드: " + ", ".join(spec.exclude_keywords)
    card = replace(card, text=text)
    table = {"title": "조건 검색", "columns": ["세트 번호", "이름", "연도", "부품 수(Rebrickable 기준)", "서브테마"],
             "rows": [[f.set_num, f.name, f.year, f.num_parts, f.subtheme] for f in rows],
             "formats": {"연도": "year", "부품 수(Rebrickable 기준)": "count"}}
    for fact in rows:
        if fact.num_parts != fact.agg_parts:
            warnings.append(f"{fact.set_num}: 부품 수(Rebrickable 기준) {fact.num_parts:,}개와 부품 목록 집계 "
                            f"{fact.agg_parts:,}개가 다릅니다 (원본 불일치).")
    answer = (f"조건에 맞는 세트는 {total:,}개이며 {len(rows):,}개를 반환합니다. [근거 1]"
              if rows else "조건에 맞는 세트가 없습니다. [근거 1]")
    return TemplateAnswer(answer, [card], [table], list(warnings))


def search_sets(question: str, *, conn: sqlite3.Connection, model: Any, meta_store: Any,
                settings: dict, on_event: Callable[[str, dict], None] | None = None,
                base_spec: FilterSpec | dict | None = None,
                allow: list[str] | tuple[str, ...] | None = None,
                keep_order: list[str] | tuple[str, ...] | bool | None = None) -> SearchResult:
    """DB가 보장한 후보만 반환하며 0건 조건은 완화하지 않는다."""
    started = perf_counter()
    cfg = settings["search"]
    for key in ("sql_candidate_limit", "review_top_n", "result_limit"):
        if type(cfg[key]) is not int or cfg[key] <= 0:
            raise ValueError("조건 탐색의 후보·검수·표시 개수는 양의 정수여야 합니다.")
    warnings: list[str] = []
    usage: dict = {}

    def event(name: str, **data: Any) -> None:
        if on_event:
            on_event(name, data)

    cache = DocStore(resolve_path(settings["paths"]["docs_dir"]),
                     resolve_path(settings["paths"]["collect_sqlite"]))
    aliases = _aliases(conn, question)
    terms = _terms(question, settings)
    event("extracting")
    query = _cached_call(model, filter_messages(question, aliases, cfg["filter_prompt_version"], terms=terms),
                         FilterQuery, cfg["filter_prompt_version"], cache, settings, usage, "filter")
    if query is None:
        warnings.append("조건 추출에 실패하여 질문 전체로 의미 탐색합니다. 수치 조건은 적용되지 않았습니다.")
        effective, discarded = FilterQuery(semantic=question.strip() or None), []
    else:
        query, discarded = _validate_conditions(query, question, aliases, warnings, terms, settings)
        effective = query
    added = _add_vehicle_conditions(effective, _vehicle_keywords(question, aliases, terms, settings))
    limit = effective.limit or (len(allow) if allow is not None else cfg["result_limit"])
    fields = ("year_from", "year_to", "parts_min", "parts_max", "subtheme", "minifig", "has_minifig")
    spec = FilterSpec(**{key: getattr(effective, key) for key in fields},
                      name_keywords=tuple(effective.name_keywords),
                      exclude_keywords=tuple(effective.exclude_keywords),
                      scopes=tuple(cfg["default_scopes"]),
                      sort="year" if effective.sort == "relevance" else effective.sort,
                      order="asc" if effective.sort == "relevance" else effective.order, limit=limit)
    spec = _refine_spec(conn, spec, base_spec, allow)
    if not (any(getattr(effective, key) is not None for key in fields)
            or effective.name_keywords or effective.exclude_keywords or effective.semantic
            or base_spec is not None or allow is not None):
        template = TemplateAnswer(
            "조건을 이해하지 못했습니다. 연도·부품 수·캐릭터·세트 종류로 다시 물어 주세요",
            [], [], list(warnings))
        result = SearchResult(query, spec, template, [], True, discarded, 0, 0, [], warnings, usage,
                              round((perf_counter() - started) * 1000), added)
        event("done", count=0, no_result=True, reviewed=0, dropped=0)
        return result
    event("querying")
    candidate_limit = max(cfg["sql_candidate_limit"], len(spec.set_nums)) if keep_order else cfg["sql_candidate_limit"]
    rows, total_sql = filter_sets(conn, replace(spec, limit=candidate_limit))
    if total_sql > len(rows):
        warnings.append(f"SQL 후보 {total_sql:,}개 중 {len(rows):,}개 안에서 탐색합니다.")
    reviewed, dropped = 0, []
    reviewed_rows: list[SetFact] = []
    total = total_sql
    if rows and effective.semantic:
        event("ranking", candidates=len(rows))
        rows = _rank(rows, question, effective.semantic, meta_store, cfg["review_top_n"], warnings)
        if rows:
            event("reviewing", candidates=len(rows))
            reviewed = len(rows)
            reviewed_rows = list(rows)
            candidates = [{"id": f.set_num, "name": f.name, "year": f.year,
                           "num_parts": f.num_parts, "set_entities": f.entities,
                           "minifig_count": sum(qty for _, qty in f.minifigs)} for f in rows]
            response = _cached_call(model, review_messages(
                question, effective.semantic, candidates, cfg["review_prompt_version"]),
                ReviewResult, cfg["review_prompt_version"], cache, settings, usage, "review")
            if response is None:
                warnings.append("의미 조건 검수에 실패하여 의미 정렬 결과를 표시합니다. 조건을 확인하세요.")
            else:
                rows, dropped = _review(rows, response, warnings)
        total = len(rows)
        warnings.append("의미 탐색 결과 수는 상위 검수 대상 안에서 집계했습니다.")
    if effective.sort != "relevance":
        rows = sorted(sorted(rows, key=lambda f: f.set_num),
                      key=lambda f: getattr(f, effective.sort), reverse=effective.order == "desc")
    if keep_order:
        order = list(allow or spec.set_nums) if keep_order is True else list(keep_order)
        positions = {number: index for index, number in enumerate(order)}
        rows.sort(key=lambda f: positions.get(f.set_num, len(positions)))
    rows = rows[:limit]
    final_sort = ("relevance" if effective.semantic and effective.sort == "relevance"
                  else f"{spec.sort} {spec.order}")
    if keep_order:
        final_sort = "이전 표시 순서"
    template = _template(rows, total, spec, warnings, final_sort)
    if not rows and dropped:
        rejected_ids = {item.id for item in dropped}
        rejected = [fact for fact in reviewed_rows if fact.set_num in rejected_ids]
        message = f"조건에 맞는 후보 {len(rejected):,}개가 있었지만 검수에서 제외됐습니다"
        details = "\n".join(f"{fact.set_num} {fact.name}" for fact in rejected)
        template.answer = f"{message}. [근거 1]\n{details}"
        template.cards = [replace(template.cards[0], text=template.cards[0].text +
                                 f"\n검수 제외 후보: {len(rejected):,}개\n{details}")]
        template.tables.append({"title": "검수에서 제외된 후보", "columns": ["세트 번호", "이름"],
                                "rows": [[fact.set_num, fact.name] for fact in rejected], "formats": {}})
    result = SearchResult(query, spec, template, [f.set_num for f in rows], not rows, discarded,
                          total_sql, reviewed, dropped, warnings, usage,
                          round((perf_counter() - started) * 1000), added)
    event("done", count=len(rows), no_result=result.no_result, reviewed=reviewed, dropped=len(dropped))
    return result


def _refine_spec(conn: sqlite3.Connection, spec: FilterSpec,
                 base: FilterSpec | dict | None, allow: list[str] | tuple[str, ...] | None) -> FilterSpec:
    """단일 필드로 표현 못 하는 조건도 DB 집합 교집합으로 보존한다."""
    allowed = None if allow is None else list(dict.fromkeys(allow))
    if base is not None:
        if isinstance(base, dict):
            base = FilterSpec(**base)
        maximum = conn.execute("SELECT COUNT(*) FROM sets").fetchone()[0]
        previous, _ = filter_sets(conn, replace(base, limit=maximum))
        previous_ids = {row.set_num for row in previous}
        allowed = ([n for n in allowed if n in previous_ids] if allowed is not None
                   else [row.set_num for row in previous])
        changes = {}
        for key in ("year_from", "parts_min", "year_to", "parts_max"):
            values = [v for v in (getattr(base, key), getattr(spec, key)) if v is not None]
            changes[key] = (max(values) if key in ("year_from", "parts_min") else min(values)) if values else None
        for key in ("name_keywords", "exclude_keywords"):
            changes[key] = tuple(dict.fromkeys((*getattr(base, key), *getattr(spec, key))))
        for key in ("subtheme", "minifig", "has_minifig"):
            changes[key] = getattr(spec, key) if getattr(spec, key) is not None else getattr(base, key)
        changes["scopes"] = tuple(s for s in base.scopes if s in spec.scopes)
        spec = replace(spec, **changes)
    if allowed is not None:
        if spec.set_nums:
            allowed = [n for n in allowed if n in spec.set_nums]
        spec = replace(spec, set_nums=tuple(allowed), scopes=spec.scopes if allowed else ())
    return spec
