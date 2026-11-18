"""LLM 없이 표시 목록·대상·미완료 요청을 해석한다. 저장이나 변경 실행은 하지 않는다."""

import re
import sqlite3
from dataclasses import dataclass, field, replace
from typing import Literal

from rag_service.config import load_settings
from rag_service.planner.sets import SetNotFound
from rag_service.services.dialogue_state import DialogueState
from rag_service.services.resolve import SetOutsideScope, contains_cue, explicit_set_numbers


@dataclass(frozen=True)
class ResolvedContext:
    subject: str | None
    subject_source: str | None
    compare: tuple[str, ...] = ()
    refine: dict | None = None
    mode: Literal["exact", "ignore_color"] | None = None
    resume: dict | None = None
    clarify: dict | None = None
    collection_target: str | None = None
    notes: tuple[str, ...] = ()
    # 저장 계층은 성공 응답의 delta를 턴과 함께 적용한다(T-024).
    state_delta: dict = field(default_factory=dict)
    summary: dict = field(default_factory=dict)
    recommend: dict | None = None
    mode_followup: bool = False
    rag_question: str | None = None


def _ordinal(question: str, cues: dict) -> int | None:
    if any(cue in question for cue in cues["last"]):
        return -1
    for pattern, rank in cues["ordinals"].items():
        if re.search(pattern, question):
            return rank - 1
    match = re.search(cues["ordinal_number"], question)
    return int(match[1]) - 1 if match else None


def _named(question: str, conn: sqlite3.Connection) -> list[str]:
    """기체를 대표 판본 하나로 축약하지 않고 실제 후보 전체를 반환한다."""
    scoped = {row[0] for row in conn.execute(
        "SELECT set_num FROM set_scope WHERE scope IN ('core', 'compare_only')")}
    found = {n for n, name in conn.execute(
        "SELECT s.set_num, s.name FROM sets s JOIN set_scope c ON c.set_num=s.set_num "
        "WHERE c.scope IN ('core', 'compare_only')")
        if name and name.casefold() in question.casefold()}
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if {"ko_aliases", "set_entities"} <= tables:
        for alias, canonical in conn.execute("SELECT alias, canonical_en FROM ko_aliases"):
            if alias and alias.casefold() in question.casefold():
                found.update(row[0] for row in conn.execute(
                    "SELECT DISTINCT set_num FROM set_entities WHERE wiki_title=?", (canonical,)))
    if "set_entities" in tables:
        for number, name in conn.execute("SELECT set_num, wiki_title FROM set_entities"):
            if name and name.casefold() in question.casefold():
                found.add(number)
    return sorted(found & scoped)


def _pending_selection(question: str, options: tuple[str, ...], cues: dict,
                       conn: sqlite3.Connection) -> str | None:
    index = _ordinal(question, cues)
    if re.fullmatch(r"\d+", question.strip()):
        index = int(question.strip()) - 1
    if index is not None:
        if index == -1 and any(c in question for c in cues["last"]):
            return options[-1] if options else None
        return options[index] if 0 <= index < len(options) else None
    names = [n for n in _named(question, conn) if n in options]
    return names[0] if len(names) == 1 else None


def resolve_context(question: str, state: DialogueState, *, conn: sqlite3.Connection,
                    settings: dict) -> ResolvedContext:
    """명시 대상 > pending 선택 > 서수 > 지시어 > 현재 대상 순으로 해석한다."""
    cues = load_settings(settings["paths"]["dialogue_cues"])
    shown = state.last_list
    items = shown.items if shown else ()
    delta = {"pending": None} if state.pending else {}
    mode = state.mode
    mode_followup = False
    if any(cue in question for cue in cues["exact_cues"]):
        mode, mode_followup = "exact", True
    elif (re.search(cues["ignore_color_pattern"], question)
          or any(cue in question for cue in cues["ignore_color_cues"])):
        mode, mode_followup = "ignore_color", True
    if mode_followup:
        delta["mode"] = mode
    refining = any(contains_cue(question, cue) for cue in cues["refine"])
    targetless = (any(contains_cue(question, cue) for cue in cues["search_cues"])
                  or re.search(cues["recommend_pattern"], question, re.I)) and not refining

    def result(subject: str | None = None, source: str | None = None, **kwargs) -> ResolvedContext:
        changes = dict(delta)
        effective_mode = mode
        if kwargs.get("resume") and not mode_followup:
            effective_mode = kwargs["resume"].get("mode", mode)
            if effective_mode != mode:
                changes["mode"] = effective_mode
        if subject and source in ("explicit", "list"):
            changes.update(subject={"set_num": subject, "source": source, "turn_id": None},
                           compare=[], pending=None)
        summary = {"subject": subject, "last_list_kind": shown.kind if shown else None,
                   "last_list_size": len(items),
                   "pending_kind": state.pending.kind if state.pending and kwargs.get("resume") else None,
                   "mode": effective_mode}
        return ResolvedContext(subject, source, mode=effective_mode, collection_target=subject,
                               state_delta=changes, summary=summary, mode_followup=mode_followup, **kwargs)

    def clarify(options: tuple[str, ...] | list[str], message: str) -> ResolvedContext:
        resolved = result(clarify={"question": message, "options": list(options), "kind": "target"})
        return replace(resolved, state_delta={"pending": None} if state.pending else {})

    try:
        numbers = explicit_set_numbers(question, conn, settings=settings)
    except SetNotFound:
        return clarify(items, "데이터에 없는 세트 번호입니다. 번호 또는 후보를 확인해 주세요.")
    except SetOutsideScope as exc:
        return clarify(items, str(exc))
    # 현재 세트의 구성품 질문에 나온 캐릭터 이름은 새 대상 지정이 아니다.
    subject_attribute = (state.subject is not None
                         and any(cue in question for cue in cues["references"])
                         and any(cue in question for cue in ("미니피그", "피규어", "minifig"))
                         and not any(cue in question for cue in settings["router"]["compare_cues"]))
    named = _named(question, conn) if not numbers and not targetless and not subject_attribute else []
    explicit = numbers or (named if len(named) == 1 else [])
    if explicit:
        comparing = any(c in question for c in (*settings["router"]["compare_cues"],
                                                *settings["router"]["set_ops_cues"]))
        if (len(explicit) == 1 and comparing and state.subject
                and any(c in question for c in cues["references"])
                and state.subject.set_num not in explicit):
            explicit.append(state.subject.set_num)
        if len(explicit) > 1 and not comparing:
            return clarify(explicit, "어느 세트인지 하나를 골라 주세요.")
        resume = None
        if (state.pending and state.pending.kind == "target" and len(explicit) == 1
                and explicit[0] in state.pending.options):
            resume = dict(state.pending.request)
        notes = ("목록 밖 세트입니다",) if refining and any(n not in items for n in explicit) else ()
        return result(explicit[0], "explicit", compare=tuple(explicit) if comparing else (),
                      resume=resume, notes=notes)

    pending = state.pending
    if pending:
        selected = _pending_selection(question, pending.options, cues, conn)
        if pending.kind == "target" and selected:
            return result(selected, "list", resume=dict(pending.request))
        text = question.strip().rstrip(".!?")
        if pending.kind == "confirm" and text in cues["yes"] + cues["no"]:
            subject = pending.options[0] if len(pending.options) == 1 else None
            return result(subject, "state", resume={**pending.request, "confirmed": text in cues["yes"]})
        if pending.kind == "quantity":
            qty_text = re.sub(r"\s*개(?:야|요)?$", "", text)
            qty = int(qty_text) if qty_text.isdecimal() else cues["quantities"].get(qty_text)
            if qty is not None and 0 < qty <= settings["collection"]["max_qty"]:
                subject = pending.options[0] if len(pending.options) == 1 else None
                return result(subject, "state", resume={**pending.request, "qty": qty})

    index = _ordinal(question, cues)
    if index is not None:
        is_last = any(c in question for c in cues["last"])
        if not items or (not is_last and not 0 <= index < len(items)):
            return clarify(items, "표시 목록의 번호를 확인해 주세요. 어느 세트를 말씀하시나요?")
        return result(items[index], "list")
    if refining:
        if not items:
            return clarify((), "정제할 목록이 없습니다. 먼저 세트를 검색하거나 추천받아 주세요.")
        return result(refine={"base_spec": shown.spec, "allow": list(items), "kind": shown.kind,
                              "rec": shown.rec})
    if targetless and not mode_followup:
        return result()
    if len(named) > 1:
        return clarify(named, "같은 이름의 세트가 여러 개입니다. 하나를 골라 주세요.")
    subject = state.subject.set_num if state.subject else None
    if any(cue in question for cue in cues["references"]):
        if subject:
            return result(subject, "state")
        if len(items) == 1:
            return result(items[0], "list")
        return clarify(items, "어느 세트를 말씀하시나요? 목록에서 하나를 골라 주세요.")
    if mode_followup and not subject:
        if shown and shown.kind == "recommend" and shown.rec:
            return result(recommend={**shown.rec, "mode": mode})
        return clarify(items, "분석할 세트나 이전 추천 목록이 필요합니다. 대상을 골라 주세요.")
    return result(subject, "state" if subject else None)
