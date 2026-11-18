"""화면에 표시된 순서를 보존하는 대화 상태와 명시적 상태 전이."""

import json
import logging
import re
from dataclasses import asdict, dataclass, replace
from typing import Literal

logger = logging.getLogger(__name__)
STATE_SCHEMA_VERSION = 1
Mode = Literal["exact", "ignore_color"]


def validate_set_num(value: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*-[1-9][0-9]*", value):
        raise ValueError("정규화된 세트 번호를 확인하세요.")


def _numbers(values: tuple[str, ...]) -> None:
    if not isinstance(values, tuple):
        raise ValueError("세트 목록은 순서가 있는 튜플이어야 합니다.")
    for value in values:
        validate_set_num(value)


def _turn_id(value: int | None) -> None:
    if value is not None and (type(value) is not int or value < 1):
        raise ValueError("턴 ID는 양의 정수여야 합니다.")


def _mapping(value: dict) -> None:
    if not isinstance(value, dict):
        raise ValueError("상태의 요청과 조건은 객체여야 합니다.")
    # JSON 왕복에서 키나 값의 타입이 바뀌는 입력도 거부한다.
    if json.loads(json.dumps(value, allow_nan=False)) != value:
        raise ValueError("상태에는 JSON 호환 값만 저장할 수 있습니다.")


@dataclass(frozen=True)
class Subject:
    set_num: str
    source: Literal["explicit", "list", "card", "ui"]
    turn_id: int | None

    def __post_init__(self) -> None:
        validate_set_num(self.set_num)
        if self.source not in ("explicit", "list", "card", "ui"):
            raise ValueError("대상 선택 출처를 확인하세요.")
        _turn_id(self.turn_id)


@dataclass(frozen=True)
class ShownList:
    kind: Literal["search", "recommend", "candidates"]
    turn_id: int | None
    items: tuple[str, ...]
    spec: dict | None = None
    rec: dict | None = None

    def __post_init__(self) -> None:
        if self.kind not in ("search", "recommend", "candidates"):
            raise ValueError("표시 목록 종류를 확인하세요.")
        _turn_id(self.turn_id)
        _numbers(self.items)
        if self.spec is not None:
            _mapping(self.spec)
        if self.rec is not None:
            _mapping(self.rec)
            if (self.rec.get("mode") not in ("exact", "ignore_color")
                    or type(self.rec.get("top_n")) is not int or self.rec["top_n"] < 1
                    or not isinstance(self.rec.get("scope"), str) or not self.rec["scope"]):
                raise ValueError("추천의 모드·개수·범위를 확인하세요.")


@dataclass(frozen=True)
class Pending:
    kind: Literal["target", "quantity", "confirm"]
    options: tuple[str, ...]
    request: dict

    def __post_init__(self) -> None:
        if self.kind not in ("target", "quantity", "confirm"):
            raise ValueError("되묻기 종류를 확인하세요.")
        _numbers(self.options)
        _mapping(self.request)
        if any(not isinstance(self.request.get(key), str) for key in ("intent", "question")):
            raise ValueError("되묻기에는 원래 의도와 질문이 필요합니다.")
        if "mode" in self.request and self.request["mode"] not in ("exact", "ignore_color"):
            raise ValueError("분석 모드를 확인하세요.")
        if "qty" in self.request and (type(self.request["qty"]) is not int or self.request["qty"] < 0):
            raise ValueError("수량은 0 이상의 정수여야 합니다.")


@dataclass(frozen=True)
class DialogueState:
    subject: Subject | None = None
    last_list: ShownList | None = None
    compare: tuple[str, ...] = ()
    mode: Mode = "exact"
    pending: Pending | None = None

    def __post_init__(self) -> None:
        for value, expected in ((self.subject, Subject), (self.last_list, ShownList), (self.pending, Pending)):
            if value is not None and not isinstance(value, expected):
                raise ValueError("대화 상태 형식을 확인하세요.")
        _numbers(self.compare)
        if self.mode not in ("exact", "ignore_color"):
            raise ValueError("분석 모드를 확인하세요.")

    def to_json(self) -> str:
        """중첩 딕셔너리 변경도 저장 직전에 다시 검증한다."""
        self.__post_init__()
        for value in (self.subject, self.last_list, self.pending):
            if value is not None:
                value.__post_init__()
        return json.dumps({"schema_version": STATE_SCHEMA_VERSION, **asdict(self)},
                          ensure_ascii=False, allow_nan=False)

    @classmethod
    def from_json(cls, raw: str | None) -> "DialogueState":
        if raw is None:
            return cls()
        try:
            data = json.loads(raw)
            if (not isinstance(data, dict) or type(data.get("schema_version")) is not int
                    or data.pop("schema_version") != STATE_SCHEMA_VERSION
                    or set(data) != {"subject", "last_list", "compare", "mode", "pending"}):
                raise ValueError("지원하지 않는 상태 형식")
            if data["subject"] is not None:
                data["subject"] = Subject(**data["subject"])
            for key, cls_, sequence in (("last_list", ShownList, "items"), ("pending", Pending, "options")):
                if data[key] is not None:
                    value = data[key]
                    if not isinstance(value[sequence], list):
                        raise ValueError("목록 형식 오류")
                    data[key] = cls_(**{**value, sequence: tuple(value[sequence])})
            if not isinstance(data["compare"], list):
                raise ValueError("비교 목록 형식 오류")
            return cls(**{**data, "compare": tuple(data["compare"])})
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
            logger.warning("대화 상태가 손상되었거나 지원하지 않는 버전입니다. 빈 상태로 복원합니다.")
            return cls()


def with_subject(s: DialogueState, subject: Subject) -> DialogueState:
    return replace(s, subject=subject, compare=(), pending=None)


def cleared(s: DialogueState) -> DialogueState:
    return replace(s, subject=None, compare=(), pending=None)


def with_list(s: DialogueState, shown: ShownList) -> DialogueState:
    mode = shown.rec["mode"] if shown.kind == "recommend" and shown.rec else s.mode
    return replace(s, last_list=shown, mode=mode, pending=None)


def with_pending(s: DialogueState, pending: Pending) -> DialogueState:
    shown = s.last_list
    if pending.kind == "target" and pending.options:
        if shown is None or shown.kind != "candidates" or shown.items != pending.options:
            shown = ShownList("candidates", None, pending.options)
    return replace(s, last_list=shown, pending=pending)
