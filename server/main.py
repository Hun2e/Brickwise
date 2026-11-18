"""공통 서비스를 REST와 POST SSE로 제공하는 단일 워커 앱."""

import asyncio
import mimetypes
import sys
from collections.abc import AsyncIterator
from dataclasses import asdict
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Annotated

from fastapi import Depends, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.sse import format_sse_event
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
# Windows 레지스트리가 .js를 text/plain으로 등록한 환경에서는 브라우저가 ES 모듈 실행을 거부한다.
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/css", ".css")

from rag_service.services.catalog import catalog_options
from rag_service.services.chat import (
    cited_cards, reserve_chat_request, execute_chat_request, fail_chat_request,
    create_conversation, list_conversations, get_conversation, change_subject,
    delete_conversation, set_history, legacy_threads, Busy, RequestConflict,
    StateConflict, CollectionConflict, SetNotFound, MigrationError,
)
from rag_service.services.conversations import Turn
from rag_service.services.orchestrator import log_exception
from rag_service.services.progress import STAGES
from rag_service.services.status import check_status
from rag_service.services.users import load_collection, new_user_id, save_collection
from server.deps import ApiError, PROJECT_ROOT, SERVER_SETTINGS, TurnLocks, user_id
from server.schemas import (
    AnswerOut, ChatIn, ClearOut, CollectionIn, CollectionOut, ErrorOut,
    HealthOut, SetOut, StageEvent, StatusOut, ThreadKey, ThreadOut, TurnOut, UserOut,
    ConversationOut, ConversationListOut, ConversationCreatedOut, SubjectIn, SetNumber, ChatResultOut,
)

Uid = Annotated[str, Depends(user_id)]
FAILURE_MESSAGE = "요청을 처리하지 못했습니다. 입력과 저장소 상태를 확인하고 다시 시도하세요."


class ChatStreamResponse(StreamingResponse):
    # EventSourceResponse를 라우트에 지정하면 FastAPI가 함수를 generator로 호출한다.
    # 일반 async 라우트에서 잠금/시작 오류를 헤더 전에 처리하기 위한 응답 클래스다.
    media_type = "text/event-stream"


def turn_out(turn: Turn, thread: str | None = None) -> TurnOut:
    """저장된 전체 JSON은 보존하고 화면용 필드와 실제 인용만 반환한다."""
    response = {**turn.response, "cards": cited_cards(turn.response)}
    primary = next((n for n, role in turn.subjects if role == "primary"), "general")
    return TurnOut(id=turn.id, thread=thread or primary, question=turn.question,
                   answer=AnswerOut.model_validate(response), created_at=turn.created_at,
                   saved=turn.id != 0, conversation_id=turn.conversation_id,
                   request_id=response.get("request_id"), kind=turn.kind,
                   state_summary=response.get("state_summary", {}), items=response.get("items", []),
                   action=response.get("action"), comparison_label=turn.comparison_label)


def conversation_out(data: dict) -> ConversationOut:
    return ConversationOut(**{**data, "state": asdict(data["state"]),
                              "turns": [turn_out(turn) for turn in data["turns"]]})


def event_bytes(event: str, payload: StageEvent | ChatResultOut | ErrorOut) -> bytes:
    return format_sse_event(event=event, data_str=payload.model_dump_json())


def chat_response(body: ChatIn, uid: str, locks: TurnLocks, keepalive: float) -> ChatStreamResponse:
    """검증/잠금은 헤더 전, 작업은 연결 취소와 독립적인 스레드에서 수행한다."""
    locks.acquire(uid, body.conversation_id)
    try:
        job = reserve_chat_request(uid, body.conversation_id, body.question, body.request_id)
    except Exception:
        locks.release(uid, body.conversation_id)
        raise
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[tuple[str, StageEvent | ChatResultOut | ErrorOut]] = asyncio.Queue()
    connected = Event()
    connected.set()

    def publish(event: str, payload: StageEvent | ChatResultOut | ErrorOut) -> None:
        def enqueue() -> None:
            if connected.is_set():
                queue.put_nowait((event, payload))

        if connected.is_set():
            try:
                loop.call_soon_threadsafe(enqueue)
            except RuntimeError:
                # 앱 종료로 루프가 닫혀도 서비스의 저장과 잠금 해제는 계속한다.
                pass

    def on_stage(label: str, detail: str | None) -> None:
        if label in STAGES:
            publish("stage", StageEvent(stage=label, detail=detail, index=STAGES.index(label)))

    def work() -> None:
        try:
            turn = execute_chat_request(job, on_stage=on_stage)
            terminal = ("result", ChatResultOut(turn=turn_out(turn), state=turn.response["state"],
                                               action=turn.response.get("action")))
        except Exception as exc:
            log_exception(exc)
            terminal = ("error", ErrorOut(code="chat_failed", message=FAILURE_MESSAGE))
        finally:
            locks.release(uid, body.conversation_id)
        publish(*terminal)

    async def stream() -> AsyncIterator[bytes]:
        try:
            while True:
                try:
                    event, payload = await asyncio.wait_for(queue.get(), timeout=keepalive)
                except TimeoutError:
                    yield format_sse_event(comment="keep-alive")
                    continue
                yield event_bytes(event, payload)
                if event in ("result", "error"):
                    return
        finally:
            connected.clear()

    try:
        Thread(target=work, name="chat-turn", daemon=False).start()
    except Exception:
        connected.clear()
        locks.release(uid, body.conversation_id)
        if not job.replay:
            fail_chat_request(job)
        raise
    # 명시적 응답으로 반환해야 generator 시작 전 4xx 검증을 마칠 수 있다.
    return ChatStreamResponse(stream(), headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def create_app() -> FastAPI:
    # SSE 라우트에서도 처리 전 오류는 application/json이다.
    errors = {code: {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorOut"}}}}
              for code in (400, 404, 405, 409, 410, 422, 500, 503)}
    app = FastAPI(title="LEGO Archive & Build Planner API", responses=errors)
    app.state.turn_locks = TurnLocks()
    app.state.server_settings = dict(SERVER_SETTINGS)
    catalog: list[SetOut] | None = None
    catalog_lock = Lock()

    if SERVER_SETTINGS["cors_origins"]:
        app.add_middleware(CORSMiddleware, allow_origins=SERVER_SETTINGS["cors_origins"],
                           allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
                           allow_headers=["Content-Type", "X-User-Id"])

    @app.exception_handler(ApiError)
    async def api_error(request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(ErrorOut(code=exc.code, message=exc.message).model_dump(), status_code=exc.status_code)

    async def service_error(request: Request, exc: Exception) -> JSONResponse:
        messages = {
            Busy: (409, "busy", "이전 질문을 처리 중입니다. 답변이 끝난 뒤 다시 질문하세요."),
            RequestConflict: (409, "request_conflict", "다른 대화에 사용한 요청 ID입니다. 새 ID로 제출하세요."),
            StateConflict: (409, "state_conflict", "대화 상태가 변경되었습니다. 다시 불러오세요."),
            CollectionConflict: (409, "collection_conflict", "보유 목록이 변경되었습니다. 다시 불러오세요."),
            SetNotFound: (404, "set_not_found", "데이터에 없는 세트입니다. 세트 번호를 확인하세요."),
            KeyError: (404, "not_found", "대화를 찾을 수 없습니다. 대화 목록을 다시 불러오세요."),
            MigrationError: (503, "migration_required", "대화 저장소 이관이 필요합니다. 관리자에게 백업 후 이관을 요청하세요."),
        }
        status, code, message = messages[type(exc)]
        return JSONResponse(ErrorOut(code=code, message=message).model_dump(), status_code=status)

    for exception in (Busy, RequestConflict, StateConflict, CollectionConflict, SetNotFound, KeyError, MigrationError):
        app.add_exception_handler(exception, service_error)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Pydantic 오류의 input/ctx는 질문 원문과 내부 예외를 포함할 수 있다.
        return JSONResponse(ErrorOut(code="invalid_request", message="요청 형식이 올바르지 않습니다. 질문 길이·대화 키·선택 세트·수량을 확인하세요.").model_dump(), status_code=422)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse(ErrorOut(code="http_error", message="요청 경로와 HTTP 메서드를 확인하세요.").model_dump(), status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        log_exception(exc)
        return JSONResponse(ErrorOut(code="server_error", message=FAILURE_MESSAGE).model_dump(), status_code=500)

    @app.get("/api/health", response_model=HealthOut)
    def health() -> HealthOut:
        return HealthOut()

    @app.get("/api/status", response_model=StatusOut)
    def status() -> dict:
        return asdict(check_status())

    @app.post("/api/users", response_model=UserOut)
    def users() -> UserOut:
        return UserOut(uid=new_user_id())

    @app.get("/api/sets", response_model=list[SetOut])
    def sets(response: Response) -> list[SetOut]:
        nonlocal catalog
        with catalog_lock:
            if catalog is None:
                catalog = [SetOut(**asdict(option), label=option.label()) for option in catalog_options()]
        response.headers["Cache-Control"] = f"max-age={SERVER_SETTINGS['catalog_max_age_sec']}"
        return catalog

    @app.get("/api/threads", response_model=list[ThreadOut])
    def threads(uid: Uid) -> list[dict]:
        return legacy_threads(uid)

    @app.get("/api/threads/{thread}/turns", response_model=list[TurnOut])
    def turns(thread: ThreadKey, uid: Uid) -> list[TurnOut]:
        return [turn_out(turn, thread) for turn in legacy_threads(uid, thread)]

    @app.delete("/api/threads/{thread}", response_model=ClearOut)
    def clear(thread: ThreadKey, uid: Uid) -> ClearOut:
        raise ApiError(410, "legacy_read_only", "이전 대화 API는 읽기 전용입니다. /api/conversations를 사용하세요.")

    @app.api_route("/api/threads", methods=["POST", "PUT", "PATCH", "DELETE"], include_in_schema=False)
    @app.api_route("/api/threads/{thread:path}", methods=["POST", "PUT", "PATCH", "DELETE"], include_in_schema=False)
    def legacy_write(uid: Uid) -> None:
        raise ApiError(410, "legacy_read_only", "이전 대화 API는 읽기 전용입니다. /api/conversations를 사용하세요.")

    @app.post("/api/conversations", response_model=ConversationCreatedOut)
    def new_conversation(uid: Uid) -> dict:
        return {"id": create_conversation(uid)}

    @app.get("/api/conversations", response_model=list[ConversationListOut])
    def conversations(uid: Uid) -> list[dict]:
        return [{**row, "subject": asdict(row["subject"]) if row["subject"] else None}
                for row in list_conversations(uid)]

    @app.get("/api/conversations/{cid}", response_model=ConversationOut)
    def conversation(cid: str, uid: Uid) -> ConversationOut:
        return conversation_out(get_conversation(uid, cid))

    @app.patch("/api/conversations/{cid}/subject", response_model=ConversationOut)
    def subject(cid: str, body: SubjectIn, uid: Uid) -> ConversationOut:
        app.state.turn_locks.acquire(uid, cid)
        try:
            return conversation_out(change_subject(uid, cid, body.set_num, body.version))
        finally:
            app.state.turn_locks.release(uid, cid)

    @app.delete("/api/conversations/{cid}", response_model=ClearOut)
    def delete(cid: str, uid: Uid) -> ClearOut:
        app.state.turn_locks.acquire(uid, cid)
        try:
            return ClearOut(deleted=delete_conversation(uid, cid))
        finally:
            app.state.turn_locks.release(uid, cid)

    @app.get("/api/sets/{set_num}/history", response_model=list[TurnOut])
    def history(set_num: SetNumber, uid: Uid) -> list[TurnOut]:
        return [turn_out(turn) for turn in set_history(uid, set_num)]

    @app.get("/api/collection", response_model=CollectionOut)
    def collection(uid: Uid) -> dict:
        return load_collection(uid)

    @app.put("/api/collection", response_model=CollectionOut)
    def replace_collection(body: CollectionIn, uid: Uid) -> dict:
        try:
            save_collection(uid, body.sets, expected_version=body.version)
        except ValueError:
            raise ApiError(422, "invalid_collection", "목록의 세트와 1 이상의 정수 수량을 입력하세요.") from None
        return load_collection(uid)

    @app.post("/api/chat", response_class=ChatStreamResponse, responses={
        200: {"model": StageEvent | ChatResultOut | ErrorOut,
              "content": {"text/event-stream": {"schema": {"type": "object"}}},
              "description": "SSE: stage* → result {turn, state, action} 또는 error 1개. 같은 request_id는 저장된 결과를 재전송."},
    })
    async def chat(body: ChatIn, uid: Uid) -> ChatStreamResponse:
        return chat_response(body, uid, app.state.turn_locks, app.state.server_settings["keepalive_sec"])

    app.mount("/", StaticFiles(directory=PROJECT_ROOT / SERVER_SETTINGS["static_dir"], html=True), name="web")
    return app


app = create_app()
