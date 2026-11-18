"""평가 JSONL 검증. 기존 라우팅 문항의 추가 필드도 보존한다."""

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, ValidationError

from rag_service.rag.schemas import Route


class Case(BaseModel):
    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)
    id: StrictStr = Field(min_length=1)
    question: StrictStr = Field(min_length=1)


class Label(BaseModel):
    doc_id: StrictStr = Field(min_length=1)
    section_path: StrictStr


class RetrievalCase(Case):
    set_num: StrictStr = Field(min_length=1)
    lang: Literal["ko", "en"]
    relevant: list[Label] = Field(min_length=1)
    kind: Literal["entity", "series", "set"]
    source: Literal["draft-llm", "pm"]
    note: StrictStr = ""


class RoutingCase(Case):
    selected_set: StrictStr | None
    history: list[dict]
    has_collection: StrictBool
    expected_route: Route
    tags: list[StrictStr] = Field(default_factory=list)


class Fact(BaseModel):
    field: StrictStr = Field(min_length=1)
    value: int | float | str | bool | None


class CompareCase(Case):
    set_num: StrictStr = Field(min_length=1)
    type: Literal["fact", "compare", "narrative", "unanswerable"]
    facts: list[Fact]
    note: StrictStr = ""


SCHEMAS = {"retrieval": RetrievalCase, "routing": RoutingCase, "rag_compare": CompareCase}


def validate_rows(rows: list[dict], kind: str) -> list[dict]:
    """빈 평가셋·중복 ID·필드 오류는 채점 전에 거절한다."""
    if not rows:
        raise ValueError("평가 문항이 없습니다. PM 검수 파일을 준비하세요.")
    result, seen = [], set()
    for index, row in enumerate(rows, 1):
        try:
            parsed = SCHEMAS[kind].model_validate(row).model_dump()
        except (ValidationError, KeyError):
            raise ValueError(f"평가 {index}번 문항의 필드 형식을 확인하세요.") from None
        if parsed["id"] in seen:
            raise ValueError(f"평가 {index}번 문항의 ID가 중복됩니다.")
        seen.add(parsed["id"])
        result.append(parsed)
    return result


def read_cases(path: Path, kind: str) -> list[dict]:
    """호출자가 지정한 파일 하나만 읽는다."""
    try:
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    except json.JSONDecodeError:
        raise ValueError("평가 파일의 JSONL 형식을 확인하세요.") from None
    return validate_rows(rows, kind)
