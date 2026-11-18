"""REST와 SSE에서 공개하는 화면용 API 계약."""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, AfterValidator, field_validator

from rag_service.services.conversations import validate_thread
from server.deps import SERVER_SETTINGS


def thread_key(value: str) -> str:
    validate_thread(value)
    return value


def set_number(value: str) -> str:
    validate_thread(value)
    if value in ("explore", "collection"):
        raise ValueError("세트 번호를 입력하세요.")
    return value


ThreadKey = Annotated[str, AfterValidator(thread_key)]
SetNumber = Annotated[str, AfterValidator(set_number)]
Quantity = Annotated[int, Field(strict=True, ge=1)]


class CardOut(BaseModel):
    n: int
    source_type: Literal["set_meta", "db", "wikipedia", "brickipedia"]
    title: str
    url: str
    license: str
    text: str
    item_id: str = ""
    doc_id: str = ""
    lang: str = ""
    section_path: str = ""


class TableOut(BaseModel):
    title: str
    columns: list[str]
    rows: list[list[Any]]
    formats: dict[str, str] = Field(default_factory=dict)


class ItemOut(BaseModel):
    rank: int
    set_num: str
    name: str
    year: int
    num_parts: int
    missing: int | None = None
    lots_missing: int | None = None


class ActionOut(BaseModel):
    set_num: str
    name: str
    op: str
    qty_before: int
    qty_after: int
    applied: bool
    replayed: bool
    needs_confirm: bool
    version: int


class StateSummaryOut(BaseModel):
    subject: str | None = None
    subject_name: str | None = None
    last_list_kind: str | None = None
    last_list_size: int = 0
    mode: Literal["exact", "ignore_color"] = "exact"
    pending: str | None = None
    version: int = 0


class SubjectIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    set_num: SetNumber | None
    version: int = Field(strict=True, ge=0)


class AnswerOut(BaseModel):
    route: str
    answer: str
    cards: list[CardOut]
    tables: list[TableOut]
    set_nums: list[str]
    needs_input: str | None
    warnings: list[str]
    timings: dict[str, int]
    items: list[ItemOut] = Field(default_factory=list)


class TurnOut(BaseModel):
    id: int
    thread: str
    question: str
    answer: AnswerOut
    created_at: str
    saved: bool
    conversation_id: str | None = None
    request_id: str | None = None
    kind: str = "answer"
    state_summary: StateSummaryOut = Field(default_factory=StateSummaryOut)
    items: list[ItemOut] = Field(default_factory=list)
    action: ActionOut | None = None
    comparison_label: str | None = None


class ConversationOut(BaseModel):
    id: str
    title: str
    turns: list[TurnOut]
    state: dict[str, Any]
    version: int
    state_summary: StateSummaryOut


class ConversationListOut(BaseModel):
    id: str
    title: str
    updated_at: str
    subject: dict[str, Any] | None
    turns: int


class ConversationCreatedOut(BaseModel):
    id: str


class ChatResultOut(BaseModel):
    turn: TurnOut
    state: dict[str, Any]
    action: ActionOut | None = None


class ChatIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    question: str = Field(min_length=1, max_length=SERVER_SETTINGS["max_question_chars"])

    @field_validator("question", "conversation_id", "request_id")
    @classmethod
    def nonblank_question(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("질문을 입력하세요.")
        return value


class StageEvent(BaseModel):
    stage: str
    detail: str | None
    index: int


class ErrorOut(BaseModel):
    code: str
    message: str


class HealthOut(BaseModel):
    status: Literal["ok"] = "ok"


class UserOut(BaseModel):
    uid: str


class StatusOut(BaseModel):
    db_ok: bool
    set_count: int
    meta_chunks: int
    doc_chunks: int
    api_key_set: bool
    user_db_ok: bool
    messages: list[str]
    can_chat: bool


class SetOut(BaseModel):
    set_num: str
    name: str
    year: int
    num_parts: int
    scope: str
    aliases: list[str]
    img_url: str
    label: str


class ThreadOut(BaseModel):
    thread: str
    turns: int
    updated_at: str


class ClearOut(BaseModel):
    deleted: int


class CollectionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sets: list[tuple[SetNumber, Quantity]]
    version: int = Field(strict=True, ge=0)


class CollectionOut(BaseModel):
    id: str
    sets: list[tuple[str, int]]
    adjustments: list[tuple[str, int, int]]
    version: int
