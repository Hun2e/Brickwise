"""대상 해석과 분리된 결정적 보유 의도 파싱 및 컬렉션 변경 서비스."""

import re
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass
from typing import Literal

from rag_service.config import load_settings, resolve_path
from rag_service.planner.collections import CollectionConflict, CollectionStore, validate_quantity
from rag_service.planner.sets import SetNotFound, get_set
from rag_service.services import users


@dataclass(frozen=True)
class CollectionIntent:
    kind: Literal["own", "wish"]
    op: Literal["set", "add", "remove", "none"]
    qty: int | None
    statement: bool


@dataclass(frozen=True)
class ActionResult:
    set_num: str
    name: str
    op: str
    qty_before: int
    qty_after: int
    applied: bool
    replayed: bool
    needs_confirm: bool
    version: int


def _quantity(question: str, rules: dict) -> int | None:
    """세트 번호는 수량으로 읽지 않고 여러 수량·소수는 거절한다."""
    if re.search(rules["unsupported_quantity"], question):
        return None
    numbers = re.findall(rules["number_quantity"], question)
    words = "|".join(sorted(map(re.escape, rules["quantities"]), key=len, reverse=True))
    korean = re.findall("(" + words + ")" + rules["quantity_suffix"], question)
    if len(numbers) + len(korean) != 1:
        return None
    if korean:
        return rules["quantities"][korean[0]]
    raw = re.sub(r"\s*개$", "", numbers[0])
    return int(raw) if re.fullmatch(r"\d+", raw) else None


def parse_intent(question: str, settings: dict | None = None) -> CollectionIntent | None:
    """소망·부정은 변경하지 않으며 명시적 수량 없는 등록·추가는 추측하지 않는다."""
    if not isinstance(question, str) or not question.strip():
        return None
    settings = load_settings() if settings is None else settings
    rules = load_settings(settings["paths"]["collection_intents"])

    def matches(key: str) -> bool:
        return any(re.search(pattern, question) for pattern in rules[key])

    if matches("wish"):
        return CollectionIntent("wish", "none", None, False)
    if matches("negative") or matches("question"):
        return None
    if matches("remove"):
        return CollectionIntent("own", "remove", None, False)
    qty = _quantity(question, rules)
    if qty is None or not 1 <= qty <= settings["collection"]["max_qty"]:
        return None
    if matches("set") or matches("correction"):
        return CollectionIntent("own", "set", qty, False)
    if matches("add"):
        return CollectionIntent("own", "add", qty, False)
    if matches("statement"):
        return CollectionIntent("own", "set", qty, True)
    return None


def apply(uid: str, *, set_num: str, op: str, qty: int | None, request_id: str,
          statement: bool = False, confirm: bool = False) -> ActionResult:
    """참조 DB는 읽기 전용으로 검증하고 사용자 DB만 원자적으로 갱신한다."""
    cid = users.collection_id_for(uid)
    settings = load_settings()
    maximum = settings["collection"]["max_qty"]
    if op != "remove" or qty is not None:
        validate_quantity(qty, maximum)
    path = resolve_path(settings["paths"]["sqlite"])
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
        info = get_set(conn, set_num)
        change = CollectionStore(users.user_db_path()).apply_change(
            cid, info.set_num, op, qty, request_id, statement=statement, confirm=confirm, max_qty=maximum,
        )
        if change.set_num != info.set_num:
            info = get_set(conn, change.set_num)
    return ActionResult(name=info.name, **asdict(change))


def describe(r: ActionResult) -> str:
    """저장 결과만 사용하는 화면 공통 안내."""
    if r.needs_confirm:
        return f"지금 {r.qty_before}개로 기록돼 있어요. {r.qty_after}개로 줄일까요?"
    if not r.applied:
        return "보유 목록을 변경하지 않았어요."
    verb = {"set": "등록", "add": "추가", "remove": "삭제"}[r.op]
    text = f"{r.set_num} {r.name}: 보유 {r.qty_before}개 → {r.qty_after}개로 {verb}했어요."
    return text + (" 이미 처리한 요청이에요." if r.replayed else "")
