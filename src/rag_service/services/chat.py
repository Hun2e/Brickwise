"""대화 이력·오케스트레이터·영속 저장을 묶은 두 UI 공용 진입점."""

from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone, timedelta
from contextlib import closing
import json
import re
import sqlite3

from rag_service.config import load_settings, resolve_path
from rag_service.rag.citations import filter_cited
from rag_service.rag.evidence import Card
from rag_service.services import orchestrator
from rag_service.services.conversations import (
    ConversationStore, ThreadKey, Turn, conversation_store, history_for, validate_thread,
)
from rag_service.services.progress import StageTracker
from rag_service.services.users import collection_id_for
from rag_service.services import users, collection_actions
from rag_service.services.conversations import ConversationRepo, StateConflict
from rag_service.services.context_resolver import resolve_context, ResolvedContext
from rag_service.services.dialogue_state import DialogueState, Subject, cleared, with_subject
from rag_service.planner.sets import get_set, SetNotFound
from rag_service.planner.collections import CollectionConflict
from rag_service.services.migrations import MigrationError


def response_to_dict(r: orchestrator.OrchestratorResponse) -> dict:
    """전체 응답과 카드의 필드 이름을 보존하는 JSON 계약."""
    return asdict(r)


def cited_cards(response: dict) -> list[dict]:
    """복원된 응답에서도 실제 인용된 번호의 카드만 표시한다."""
    cards = [Card(**card) for card in response.get("cards", [])]
    cited, _, _ = filter_cited(response.get("answer", ""), cards)
    return [asdict(card) for card in cited]


def chat_turn(uid: str, thread: ThreadKey, question: str, *, selected_set: str | None,
              on_stage: Callable[[str, str | None], None] | None = None,
              store: ConversationStore | None = None) -> Turn:
    cid = collection_id_for(uid)
    validate_thread(thread)
    if (thread in ("explore", "collection") and selected_set is not None
            or thread not in ("explore", "collection") and selected_set != thread):
        raise ValueError("선택 세트와 대화 키를 일치시켜 주세요.")
    settings = load_settings()
    warnings: list[str] = []
    history = []
    try:
        if store is None:
            store = conversation_store()
        n = settings["retrieval"]["history_turns"]
        history = history_for(store.turns(uid, thread, limit=n), n)
    except Exception as exc:
        orchestrator.log_exception(exc, settings)
        warnings.append("이전 대화를 읽지 못했습니다. 저장소 상태를 확인하세요.")

    tracker = StageTracker()

    def event(name: str, data: dict) -> None:
        stage = tracker.update(name, data)
        if stage is not None and on_stage is not None:
            on_stage(*stage)

    response = response_to_dict(orchestrator.answer(
        question, selected_set=selected_set, history=history, collection_id=cid, on_event=event,
    ))
    response["warnings"].extend(warnings)
    try:
        if store is None:
            store = conversation_store()
        return store.append(uid, thread, question, response)
    except Exception as exc:
        orchestrator.log_exception(exc, settings)
        response["warnings"].append("답변을 저장하지 못했습니다. 새로고침 전에 내용을 보관하고 저장소 상태를 확인하세요.")
        return Turn(0, question, response, datetime.now(timezone.utc).isoformat())


class Busy(Exception):
    """진행 중인 요청은 같은 ID로 완료 후 다시 조회한다."""


class RequestConflict(Exception):
    """요청 ID는 한 대화의 한 제출에만 사용한다."""


def conversation_repo() -> ConversationRepo:
    return ConversationRepo(users.user_db_path())


def _reference_db():
    path = resolve_path(load_settings()["paths"]["sqlite"])
    return closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True))


def state_summary(state: DialogueState, version: int) -> dict:
    name = None
    if state.subject:
        try:
            with _reference_db() as conn:
                name = get_set(conn, state.subject.set_num).name
        except (sqlite3.Error, SetNotFound):
            pass
    pending_text = {"target": "대상 세트를 골라 주세요.", "quantity": "보유 수량을 알려 주세요.",
                    "confirm": "보유 수량 변경을 확인해 주세요."}
    return {"subject": state.subject.set_num if state.subject else None, "subject_name": name,
            "last_list_kind": state.last_list.kind if state.last_list else None,
            "last_list_size": len(state.last_list.items) if state.last_list else 0,
            "mode": state.mode, "pending": pending_text[state.pending.kind] if state.pending else None,
            "version": version}


def create_conversation(uid: str) -> str:
    return conversation_repo().create(uid)


def draft_conversation(set_num: str | None = None) -> dict:
    """첫 질문 전 화면에 쓸 상태. 대화나 사용자 DB 행은 생성하지 않는다."""
    state = DialogueState(subject=Subject(set_num, "ui", None) if set_num else None)
    return {"id": None, "title": "새 대화", "turns": [], "state": state, "version": 0,
            "state_summary": state_summary(state, 0)}


def list_conversations(uid: str) -> list[dict]:
    return conversation_repo().list(uid)


def get_conversation(uid: str, conversation_id: str) -> dict:
    result = conversation_repo().get(uid, conversation_id)
    result["state_summary"] = state_summary(result["state"], result["version"])
    return result


def change_subject(uid: str, conversation_id: str, set_num: str | None, version: int) -> dict:
    repo = conversation_repo()
    if set_num is not None:
        with _reference_db() as conn:
            number = get_set(conn, set_num).set_num
    # T-021에는 턴 없는 상태 변경 함수가 없어 동일 CAS 규칙으로 갱신한다.
    with closing(sqlite3.connect(repo.path)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        current = repo.get(uid, conversation_id)
        if current["version"] != version:
            raise StateConflict()
        state = (cleared(current["state"]) if set_num is None
                 else with_subject(current["state"], Subject(number, "ui", None)))
        now = datetime.now(timezone.utc).isoformat()
        conn.execute("UPDATE conversation_state SET state_json=?, version=version+1, updated_at=? "
                     "WHERE conversation_id=? AND version=?", (state.to_json(), now, conversation_id, version))
        conn.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, conversation_id))
    return get_conversation(uid, conversation_id)


def delete_conversation(uid: str, conversation_id: str) -> int:
    return conversation_repo().delete(uid, conversation_id)


def set_history(uid: str, set_num: str) -> list[Turn]:
    return conversation_repo().set_history(uid, set_num)


def legacy_threads(uid: str, thread: str | None = None) -> list:
    """이관 출처가 있는 대화만 구 읽기 API에 표시한다."""
    repo = conversation_repo()
    collection_id_for(uid)
    with closing(sqlite3.connect(repo.path)) as conn:
        rows = conn.execute("SELECT id, legacy_thread, updated_at FROM conversations "
                            "WHERE uid=? AND legacy_thread IS NOT NULL ORDER BY updated_at DESC", (uid,)).fetchall()
    if thread is not None:
        validate_thread(thread)
        return [turn for cid, key, _ in rows if key == thread for turn in repo.get(uid, cid)["turns"]]
    return [{"thread": key, "turns": len(repo.get(uid, cid)["turns"]), "updated_at": at}
            for cid, key, at in rows]


@dataclass(frozen=True)
class ChatRequest:
    uid: str
    conversation_id: str
    question: str
    request_id: str
    repo: ConversationRepo
    replay: Turn | None = None

    @property
    def storage_id(self) -> str:
        # 하위 테이블의 UNIQUE(request_id)를 사용자별 멱등 계약에 맞춘다.
        return f"{self.uid}:{self.request_id}"


def reserve_chat_request(uid: str, conversation_id: str, question: str, request_id: str) -> ChatRequest:
    """SSE 헤더 전에 요청을 등록하며, 턴 저장 직후 중단된 장부도 복구한다."""
    collection_id_for(uid)
    if not isinstance(question, str) or not question.strip() or not isinstance(request_id, str) or not request_id.strip():
        raise ValueError("질문과 요청 ID를 입력하세요.")
    repo = conversation_repo()
    job = ChatRequest(uid, conversation_id, question, request_id, repo)
    with closing(sqlite3.connect(repo.path)) as conn, conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        current = repo.get(uid, conversation_id)
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(seconds=load_settings()["chat"]["request_timeout_sec"])
        conn.execute("UPDATE chat_requests SET status='failed' WHERE uid=? AND conversation_id=? "
                     "AND status='running' AND julianday(created_at)<=julianday(?)",
                     (uid, conversation_id, cutoff.isoformat()))
        row = conn.execute("SELECT conversation_id, status FROM chat_requests WHERE uid=? AND request_id=?",
                           (uid, request_id)).fetchone()
        if row and row[0] != conversation_id:
            raise RequestConflict()
        saved = next((t for t in current["turns"] if t.request_id == job.storage_id), None)
        if saved:
            conn.execute("UPDATE chat_requests SET status='done', turn_id=? WHERE uid=? AND request_id=?",
                         (saved.id, uid, request_id))
            return replace(job, replay=saved)
        if row and row[1] in ("running", "done"):
            raise Busy()
        if conn.execute("SELECT 1 FROM chat_requests WHERE uid=? AND conversation_id=? AND status='running'",
                        (uid, conversation_id)).fetchone():
            raise Busy()
        conn.execute("INSERT INTO chat_requests(uid, request_id, conversation_id, status, created_at) "
                     "VALUES (?, ?, ?, 'running', ?) ON CONFLICT(uid, request_id) DO UPDATE "
                     "SET status='running', created_at=excluded.created_at",
                     (uid, request_id, conversation_id, now.isoformat()))
    return job


def fail_chat_request(job: ChatRequest) -> None:
    with closing(sqlite3.connect(job.repo.path)) as conn, conn:
        conn.execute("UPDATE chat_requests SET status='failed' WHERE uid=? AND request_id=? AND status='running'",
                     (job.uid, job.request_id))


def _merge_state(state: DialogueState, delta: dict) -> DialogueState:
    return DialogueState.from_json(json.dumps({**json.loads(state.to_json()), **delta}))


def _state_snapshot(state: DialogueState) -> dict:
    payload = json.loads(state.to_json())
    payload.pop("schema_version")
    return payload


def _action_request(question: str, ctx: ResolvedContext, settings: dict) -> dict | None:
    if ctx.resume and ctx.resume["intent"] in ("own", "wish"):
        return dict(ctx.resume)
    intent = collection_actions.parse_intent(question, settings)
    if intent:
        return {"intent": intent.kind, "op": intent.op, "qty": intent.qty,
                "statement": intent.statement, "question": question}
    # parse_intent는 수량 미확정이면 None을 반환한다. 사전의 동사로 되묻기만 만든다.
    rules = load_settings(settings["paths"]["collection_intents"])
    if any(re.search(p, question) for key in ("wish", "negative", "question") for p in rules[key]):
        return None
    for key, op in (("set", "set"), ("correction", "set"), ("add", "add")):
        matches = [m for p in rules[key] for m in re.finditer(p, question)]
        if key == "add":
            # "더 설명", "추가 설명"은 수량이 생략된 보유 명령이 아니다.
            matches = [m for m in matches if m.group() != "더"
                       and (m.group() != "추가" or re.match(r"\s*하|\s*해", question[m.end():]))]
        if matches:
            return {"intent": "own", "op": op, "question": question}
    return None


def _clarification(message: str, kind: str, options: list[str], request: dict,
                   ctx: ResolvedContext) -> dict:
    response = response_to_dict(orchestrator.OrchestratorResponse(
        answer=message, needs_input=message, state_delta=dict(ctx.state_delta)))
    response["state_delta"]["pending"] = {"kind": kind, "options": options,
        "request": {key: value for key, value in request.items() if value is not None}}
    if kind == "target" and options:
        with _reference_db() as conn:
            infos = [get_set(conn, n) for n in options]
            response["items"] = [{"rank": i, **{k: getattr(info, k)
                for k in ("set_num", "name", "year", "num_parts")}} for i, info in enumerate(infos, 1)]
        response["set_nums"] = options
        response["state_delta"]["last_list"] = {"kind": "candidates", "turn_id": None,
                                                "items": options, "spec": None, "rec": None}
    return response


def _respond(job: ChatRequest, state: DialogueState, on_stage: Callable | None) -> tuple[dict, str, ResolvedContext]:
    settings = load_settings()
    with _reference_db() as conn:
        ctx = resolve_context(job.question, state, conn=conn, settings=settings)
        # 후보 없는 대상 되묻기에도 번호만 입력해 미완료 등록을 이어갈 수 있다.
        if (state.pending and state.pending.kind == "target" and not state.pending.options
                and ctx.subject and ctx.subject_source == "explicit"
                and job.question.strip() in (ctx.subject, ctx.subject.rsplit("-", 1)[0])):
            ctx = replace(ctx, resume=dict(state.pending.request))
    request = _action_request(job.question, ctx, settings)
    tracker = StageTracker()

    def event(name: str, data: dict) -> None:
        stage = tracker.update(name, data)
        if stage and on_stage:
            on_stage(*stage)

    def answer(question: str, context: ResolvedContext) -> dict:
        n = settings["retrieval"]["history_turns"]
        history = history_for(job.repo.context_turns(job.uid, job.conversation_id, context.subject, n), n)
        # 원문은 job에 남기고, 지시어·서수가 가리키는 DB 이름을 P1/P3에도 전달한다.
        if context.subject and not (context.refine or context.recommend or context.mode_followup):
            with _reference_db() as conn:
                target = get_set(conn, context.subject)
            original = context.resume["question"] if context.resume else question
            context = replace(context, rag_question=f"{original}\n대상 세트: {target.set_num} {target.name}")
        return response_to_dict(orchestrator.answer(question, selected_set=context.subject, history=history,
            collection_id=collection_id_for(job.uid), context=context, on_event=event))

    if ctx.clarify:
        clarification = ctx.clarify
        return (_clarification(clarification["question"], clarification["kind"], clarification["options"],
            request or {"intent": "narrative", "question": job.question}, ctx), "clarify", ctx)
    if not request:
        response = answer(job.question, ctx)
        return response, "clarify" if response["needs_input"] else "answer", ctx
    response = response_to_dict(orchestrator.OrchestratorResponse(route="collection", state_delta=dict(ctx.state_delta)))
    if request["intent"] == "wish" or request.get("confirmed") is False:
        response["answer"] = "보유 목록은 그대로예요. 등록하려면 수량과 함께 '등록해줘'라고 말해 주세요."
        return response, "answer", ctx
    target = ctx.collection_target
    if not target:
        options = list(state.last_list.items) if state.last_list else []
        return _clarification("등록할 세트를 골라 주세요.", "target", options, request, ctx), "clarify", ctx
    if request.get("qty") is None and request["op"] != "remove":
        return _clarification("보유 수량을 정수로 알려 주세요.", "quantity", [target], request, ctx), "clarify", ctx
    action = collection_actions.apply(job.uid, set_num=target, op=request["op"], qty=request.get("qty"),
        request_id=job.storage_id, statement=request.get("statement", False), confirm=request.get("confirmed", False))
    if action.needs_confirm:
        return _clarification(collection_actions.describe(action), "confirm", [target], request, ctx), "clarify", ctx
    response.update(answer=collection_actions.describe(action), action=asdict(action), set_nums=[target])
    # 변경 다음 절만 전달하여 등록 동사가 추천·설명 라우팅에 섞이지 않게 한다.
    parts = re.split(r"(?:등록|추가|수정|삭제|제거)(?:해\s*주고|하고)|(?:넣어|빼)\s*주고", request["question"], maxsplit=1)
    if len(parts) == 2 and parts[1].strip():
        followup = parts[1].strip()
        try:
            with _reference_db() as conn:
                follow_ctx = resolve_context(followup, _merge_state(state, ctx.state_delta), conn=conn, settings=settings)
            extra = answer(followup, follow_ctx)
            if extra["needs_input"]:
                raise ValueError("후속 답변 실패")
            extra["answer"] = response["answer"] + "\n" + extra["answer"]
            extra["action"] = response["action"]
            extra["state_delta"] = {**response["state_delta"], **extra["state_delta"]}
            response = extra
        except Exception as exc:
            orchestrator.log_exception(exc, settings)
            response["answer"] += " 변경은 완료됐지만 추천·설명은 실패했어요. 추천이나 설명만 다시 요청해 주세요."
    return response, "action", ctx


def execute_chat_request(job: ChatRequest, *, on_stage: Callable | None = None) -> Turn:
    """예약된 작업은 연결 수명과 무관하게 변경·응답·상태 저장까지 완료한다."""
    if job.replay:
        return job.replay
    try:
        state, version = job.repo.load_state(job.uid, job.conversation_id)
        response, kind, ctx = _respond(job, state, on_stage)
        delta = response.get("state_delta", {})
        subjects = [(n, "compared") for n in ctx.compare]
        primary = ctx.subject
        if not primary and delta.get("subject"):
            primary = delta["subject"]["set_num"]
        if primary and not subjects:
            subjects.append((primary, "primary"))
        subjects.extend((item["set_num"], "listed") for item in response.get("items", []))
        response["request_id"] = job.request_id
        for attempt in range(2):
            next_state = _merge_state(state, delta)
            response["state"] = _state_snapshot(next_state)
            response["state_summary"] = state_summary(next_state, version + 1)
            try:
                turn = job.repo.save_turn_with_state(job.uid, job.conversation_id, question=job.question,
                    response=response, kind=kind, subjects=subjects, state=next_state,
                    expected_version=version, request_id=job.storage_id)
                with closing(sqlite3.connect(job.repo.path)) as conn, conn:
                    conn.execute("UPDATE chat_requests SET status='done', turn_id=? WHERE uid=? AND request_id=?",
                                 (turn.id, job.uid, job.request_id))
                return turn
            except StateConflict:
                state, version = job.repo.load_state(job.uid, job.conversation_id)
                if attempt == 0:
                    continue
                response["warnings"].append("상태 저장 충돌: 답변을 보관하고 대화를 다시 불러오세요.")
            except Exception as exc:
                orchestrator.log_exception(exc, load_settings())
                response["warnings"].append("답변을 저장하지 못했습니다. 내용을 보관하고 다시 시도하세요.")
            break
        fail_chat_request(job)
        response["state"] = _state_snapshot(state)
        response["state_summary"] = state_summary(state, version)
        return Turn(0, job.question, response, datetime.now(timezone.utc).isoformat(), job.conversation_id,
                    job.storage_id, kind, version, subjects)
    except Exception:
        fail_chat_request(job)
        raise


def chat_turn_v2(uid: str, conversation_id: str, question: str, *, request_id: str,
                 on_stage: Callable[[str, str | None], None] | None = None) -> Turn:
    return execute_chat_request(reserve_chat_request(uid, conversation_id, question, request_id), on_stage=on_stage)
