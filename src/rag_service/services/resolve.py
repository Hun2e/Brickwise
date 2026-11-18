"""세트 번호와 명시된 기체를 DB로만 확인한다."""

import re
import sqlite3
from dataclasses import dataclass, field

from rag_service.config import load_settings
from rag_service.planner.sets import SET_SELECT, SetInfo, SetNotFound, normalize_set_num
from rag_service.rag.schemas import RouterOutput


@dataclass
class Resolved:
    set_nums: list[str] = field(default_factory=list)
    needs_input: str | None = None
    candidates: list[SetInfo] = field(default_factory=list)


class SetOutsideScope(ValueError):
    """DB에는 있지만 서비스 범위 밖인 명시 번호."""


def contains_cue(question: str, cue: str) -> bool:
    """영문 단서는 단어 경계를 지키고 대소문자를 무시한다."""
    if cue.isascii():
        return bool(re.search(r"(?<!\w)" + re.escape(cue) + r"(?!\w)", question, re.I))
    return cue in question


def explicit_set_tokens(question: str, settings: dict | None = None) -> list[str]:
    """접미 번호와 4~6자리 번호만 추출하고 연도·수량 문맥은 제외한다."""
    cfg = (settings if settings is not None else load_settings())["router"]["set_number_year_range"]
    pattern = r"(?<![A-Za-z0-9-])(?:[A-Za-z0-9]+-\d+|\d{4,6})(?![A-Za-z0-9-])"
    tokens = []
    for match in re.finditer(pattern, question):
        raw = match.group()
        if "-" not in raw:
            if (cfg[0] <= int(raw) <= cfg[1]
                    or re.search(r"\b(?:in|since|after|before|year|from|until|during)\s*$",
                                 question[:match.start()], re.I)
                    or re.match(r"\s*(?:년|개|피스|부품\s*(?:이상|이하|미만|초과))",
                                question[match.end():])):
                continue
        tokens.append(raw)
    return tokens


def explicit_set_numbers(question: str, conn: sqlite3.Connection,
                         hints: list[str] | tuple[str, ...] = (), *,
                         settings: dict | None = None) -> list[str]:
    """질문과 라우터 번호에 같은 연도·DB·범위 검사를 적용한다."""
    settings = settings if settings is not None else load_settings()
    tokens = explicit_set_tokens(question, settings)
    for raw in hints:
        raw = raw.strip()
        stem = raw.split("-")[0]
        # 모델이 연도에 '-1'을 붙여도 원문의 연도 판정을 우회할 수 없다.
        if stem.isdigit():
            occurrences = re.findall(r"(?<![A-Za-z0-9-])" + re.escape(stem)
                                     + r"(?:-\d+)?(?![A-Za-z0-9-])", question)
            if occurrences and not any(value in tokens for value in occurrences):
                continue
            if not explicit_set_tokens(raw, settings):
                continue
        if raw not in tokens:
            tokens.append(raw)
    numbers = []
    for raw in tokens:
        number = normalize_set_num(conn, raw)
        if not conn.execute("SELECT 1 FROM set_scope WHERE set_num=? "
                            "AND scope IN ('core', 'compare_only')", (number,)).fetchone():
            raise SetOutsideScope("DB에 존재하지만 서비스 범위 밖인 세트입니다. 범위 안의 세트를 선택해 주세요.")
        if number not in numbers:
            numbers.append(number)
    return numbers


def _entity_sets(conn: sqlite3.Connection, entities: list[str], question: str) -> list[SetInfo]:
    """DB에 등록된 기체·별칭만 사용하고 여러 기체면 후보로 되묻는다."""
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "set_entities" not in tables:
        return []
    known = {row[0] for row in conn.execute("SELECT DISTINCT wiki_title FROM set_entities WHERE kind='vehicle'")}
    names = {name for name in known if name.casefold() in question.casefold()
             or any(name.casefold() == value.casefold() for value in entities)}
    if "ko_aliases" in tables:
        for alias, name in conn.execute("SELECT alias, canonical_en FROM ko_aliases WHERE kind='vehicle'"):
            if alias and (alias in question or alias in entities) and name in known:
                names.add(name)
    representatives = []
    for name in sorted(names):
        row = conn.execute(SET_SELECT + """ WHERE c.scope IN ('core', 'compare_only') AND EXISTS (
            SELECT 1 FROM set_entities e WHERE e.set_num=s.set_num
            AND e.kind='vehicle' AND e.wiki_title=?)
            ORDER BY CASE c.scope WHEN 'core' THEN 0 ELSE 1 END, s.year, s.set_num LIMIT 1""", (name,)).fetchone()
        if row:
            representatives.append(SetInfo(*row))
    return representatives


def resolve_sets(route: str, router_out: RouterOutput, question: str,
                 selected_set: str | None, conn: sqlite3.Connection, *,
                 settings: dict | None = None) -> Resolved:
    """명시 번호 → 지시어 → 필요한 선택 세트 순으로 중복 없이 해석한다."""
    result = Resolved()
    prompt = "어느 세트를 말씀하시는지 세트 번호나 이름을 알려 주세요."

    def append(raw: str) -> None:
        number = normalize_set_num(conn, raw)
        if number not in result.set_nums:
            result.set_nums.append(number)

    refers = "selected" in getattr(router_out, "set_refs", []) or any(
        word in question for word in ("이 세트", "이 기체", "그거", "그 세트", "그 기체"))
    try:
        for raw in explicit_set_numbers(question, conn, router_out.set_nums, settings=settings):
            append(raw)
        if refers:
            if not selected_set:
                result.needs_input = prompt
                return result
            append(selected_set)
        if route == "compare" and not result.set_nums:
            candidates = _entity_sets(conn, router_out.entities, question)
            if len(candidates) == 1:
                append(candidates[0].set_num)
            elif candidates:
                return Resolved([], prompt, candidates)
        required = 2 if route == "set_ops" else 1
        if route in ("fact", "narrative", "build", "set_ops", "compare"):
            if len(result.set_nums) < required and selected_set:
                append(selected_set)
            # 대상이 없는 build는 추천 경로이며 번호를 추측하지 않는다.
            if len(result.set_nums) < required and route != "build":
                result.needs_input = ("비교할 서로 다른 두 세트 번호를 알려 주세요."
                                      if route == "set_ops" else prompt)
    except SetOutsideScope as exc:
        result.set_nums = []
        result.needs_input = str(exc)
    except SetNotFound as exc:
        result.needs_input = "데이터에 없는 세트 번호입니다. 번호 또는 후보를 확인하세요. " + prompt
        result.candidates = exc.candidates
    return result
