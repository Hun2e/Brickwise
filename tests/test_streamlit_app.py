"""대화 화면 계약과 T-025 선행 정리를 임시 저장소·대역 모델로 검증한다."""
import ast
import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest
from streamlit.testing.v1 import AppTest

from rag_service.services import catalog, chat, users, status
from rag_service.services.catalog import SetOption
from rag_service.services.dialogue_state import DialogueState, ShownList, Subject, Pending
from rag_service.services.status import Status
from rag_service.rag.templates import particle
from test_chat_v2 import session, send
from test_db_queries import conn, real_conn
from test_orchestrator_offline import deps, model

APP = Path(__file__).resolve().parents[1] / "app_streamlit.py"
UID = "a" * 32
QUESTIONS = ["루크가 들어 있는 엑스윙 찾아줘", "그중 2021년에 나온 건?", "부품이 몇 개야?",
             "그거 하나 보유 목록에 등록해줘", "내 부품으로 다른 건 뭐 만들 수 있어?",
             "첫 번째 건 어떤 기체야?", "색상은 무시하면 얼마나 부족해?"]


def button(at, label):
    return next(b for b in at.button if b.label == label)


def run_editor(at, edits):
    states = at._tree.get_widget_states()
    revision = at.session_state[f"editor_revision:{UID}"]
    mapper = at.session_state._state._key_id_mapper
    states.widgets.add(id=mapper.get_id_from_key(f"owned:{UID}:{revision}"), string_value=json.dumps(edits))
    return at._run(states)


@pytest.fixture
def ui(session, monkeypatch):
    _, _, repo, _, _ = session
    options = [SetOption("A-1", "Falcon", 2020, 100, "core", ["팔콘"], ""),
               SetOption("B-1", "X-wing", 2021, 200, "core", ["엑스윙"], "")]
    monkeypatch.setattr(catalog, "catalog_options", lambda: options)
    ready = Status(True, 2, 2, 1, True, True, [], True)
    monkeypatch.setattr(status, "check_status", lambda: ready)
    calls, replay = [], {}
    cid = repo.create(UID)
    def fake(uid, cid, question, *, request_id, on_stage):
        calls.append((uid, cid, question, request_id))
        if request_id in replay:
            return replay[request_id]
        state, version = repo.load_state(uid, cid)
        for label in ["분류", "검색어 추출", "문서 수집", "검색", "생성", "검증"]:
            on_stage(label, None)
        items, subjects, action = [], [], None
        def item(n, rank=1):
            return dict(rank=rank, set_num=n, name="Falcon" if n == "A-1" else "X-wing", year=2021, num_parts=100)
        if "찾아" in question:
            items = [item("A-1"), item("B-1", 2)]
            state = replace(state, last_list=ShownList("search", None, ("A-1", "B-1")))
        if "그중" in question:
            state = replace(state, subject=Subject("A-1", "list", None))
        if "등록" in question:
            collection = users.load_collection(uid)
            before = dict(collection["sets"]).get("A-1", 0)
            users.save_collection(uid, [("A-1", 1)], expected_version=collection["version"])
            action = dict(set_num="A-1", qty_before=before, qty_after=1, applied=True)
        if "다른 건" in question:
            items = [{**item("B-1"), "missing": 12, "lots_missing": 3}]
            state = replace(state, last_list=ShownList("recommend", None, ("B-1",)))
        if "첫 번째" in question:
            state = replace(state, subject=Subject(state.last_list.items[0], "list", None))
        if "색상" in question:
            state = replace(state, mode="ignore_color")
        if question == "되묻기":
            state = replace(state, pending=Pending("target", ("B-1",), {"intent": "select", "question": question}))
        if question == "B-1":
            state = replace(state, subject=Subject("B-1", "list", None), pending=None)
        if state.subject:
            subjects = [(state.subject.set_num, "primary")]
        if question == "비교":
            subjects = [("A-1", "compared"), ("B-1", "compared")]
        response = dict(answer=f"{question} 답변 [근거 7]", route="filter" if "찾아" in question else "fact",
            cards=[dict(n=7, source_type="db", title="인용 제목", url="https://rebrickable.com/", license="Rebrickable", text="발췌 내용"),
                   dict(n=9, source_type="db", title="미인용 제목", url="https://rebrickable.com/", license="Rebrickable", text="숨김")],
            items=items, tables=[dict(title="결과 표", columns=["번호"], rows=[["A-1"]])],
            warnings=["안내 경고"], timings={"total_ms": 15}, set_nums=[], needs_input=None,
            state_summary=chat.state_summary(state, version + 1))
        if action:
            response["action"] = action
        if question == "인용 없음":
            response["answer"] = "확인되지 않음"
        if question == "탐색 결과 없음":
            response.update(route="filter", items=[])
        turn = repo.save_turn_with_state(uid, cid, question=question, response=response, kind="answer",
            subjects=subjects, state=state, expected_version=version, request_id=request_id)
        replay[request_id] = turn
        return turn
    monkeypatch.setattr(chat, "chat_turn_v2", fake)
    def app(uid=UID, conv=cid):
        at = AppTest.from_file(str(APP), default_timeout=20)
        at.query_params.update(uid=uid, conv=conv)
        return at.run()
    return app, repo, ready, calls, cid


def test_ui_import_boundary():
    tree = ast.parse(APP.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                 else [node.module] if isinstance(node, ast.ImportFrom) else [])
        for name in names:
            if name.startswith("rag_service"):
                assert name.startswith("rag_service.services.")
        if isinstance(node, ast.Attribute):
            assert node.attr not in ("execute", "executescript", "similarity_search", "invoke")


def test_seven_steps_reconnect_evidence_stages_and_pending(ui):
    app, repo, _, calls, cid = ui
    at = app()
    assert not at.exception and not at.chat_input[0].disabled
    assert any("대상 없음" in h.value for h in at.subheader)
    for q in QUESTIONS[:5]:
        at.chat_input[0].set_value(q).run()
        assert not at.exception
    assert any("0개 → 1개" in text.value for text in at.text)
    at = app(conv=at.query_params["conv"][0])
    for q in QUESTIONS[5:]:
        at.chat_input[0].set_value(q).run()
        assert not at.exception
    result = repo.get(UID, cid)
    assert len(result["turns"]) == 7 and result["state"].mode == "ignore_color"
    assert result["state"].subject.set_num == "B-1"
    assert {c[1] for c in calls} == {cid} and len({c[3] for c in calls}) == 7
    assert any(e.label == "[근거 7] db · 인용 제목" for e in at.expander)
    assert not any("미인용" in e.label for e in at.expander)
    assert any(e.label == "상세 표" for e in at.expander)
    assert [m.value for m in at.status[0].markdown] == ["분류", "검색어 추출", "문서 수집", "검색", "생성", "검증"]
    at.chat_input[0].set_value("되묻기").run()
    at = app()
    button(at, "B-1").click().run()
    assert not at.exception and repo.get(UID, cid)["state"].pending is None


def test_card_subject_changes_in_place_history_isolation_and_registration(ui):
    app, repo, _, calls, cid = ui
    at = app()
    at.chat_input[0].set_value("찾아줘").run()
    button(at, "이 세트로 질문").click().run()
    at.chat_input[0].set_value("A 질문").run()
    choices = [b for b in at.button if b.label == "이 세트로 질문"]
    choices[1].click().run()
    at.chat_input[0].set_value("B 질문").run()
    assert at.query_params["conv"] == [cid]
    assert "A 질문" not in [e.label for e in at.sidebar.expander]
    assert "B 질문" in [e.label for e in at.sidebar.expander]
    at.chat_input[0].set_value("비교").run()
    assert any("비교: A-1·B-1" in m.value for m in at.sidebar.markdown)
    button(at, "보유 등록").click().run()
    count = len(calls)
    button(at, "보유 등록").click().run()
    assert not at.exception and len(calls) == count
    assert users.load_collection(UID)["sets"] == [("A-1", 1)]
    button(at, "해제").click().run()
    assert repo.get(UID, cid)["state"].subject is None
    assert len(at.chat_message) >= 8


def test_missing_key_disables_chat_only(ui):
    app, _, ready, _, _ = ui
    ready.api_key_set = ready.can_chat = False
    ready.messages = ["API 키가 설정되지 않았습니다. 실행 환경에 GMS_KEY를 설정하세요."]
    at = app()
    assert not at.exception and at.chat_input[0].disabled
    assert not at.selectbox[0].disabled and not button(at, "컬렉션 저장").disabled
    assert any("API 키" in i.value for i in at.info)


def test_collection_editor_version_conflict_reload_and_save(ui):
    app, _, _, _, _ = ui
    at = app()
    users.save_collection(UID, [("B-1", 2)], expected_version=0)
    button(at, "컬렉션 저장").click()
    run_editor(at, {"edited_rows": {}, "added_rows": [{"세트": "A-1", "수량": 2}], "deleted_rows": []})
    assert not at.exception
    assert any("다시 불러온" in e.value for e in at.error)
    assert users.load_collection(UID)["sets"] == [("B-1", 2)]
    button(at, "컬렉션 다시 불러오기").click().run()
    button(at, "컬렉션 저장").click()
    run_editor(at, {"edited_rows": {"0": {"수량": 3}}, "added_rows": [], "deleted_rows": []})
    assert not at.exception and users.load_collection(UID)["sets"] == [("B-1", 3)]


def test_no_citation_empty_result_new_delete_and_user_isolation(ui):
    app, repo, _, _, cid = ui
    at = app()
    at.chat_input[0].set_value("인용 없음").run()
    assert any(i.value == "답변에 인용된 근거가 없습니다" for i in at.info)
    at.chat_input[0].set_value("탐색 결과 없음").run()
    assert any("조건을 바꾸어" in i.value for i in at.info)
    button(at, "새 대화").click().run()
    assert not at.exception and not at.chat_message and at.query_params["conv"] != [cid]
    assert not app("b" * 32).chat_message
    at = app()
    button(at, "이 대화 지우기").click().run()
    assert not at.exception and not at.chat_message
    with pytest.raises(KeyError):
        repo.get(UID, cid)
    assert users.valid_user_id(app("../../bad").query_params["uid"][0])


def test_draft_open_reset_and_subject_selection_do_not_create_conversations(ui):
    app, repo, _, _, _ = ui
    uid = "c" * 32
    at = app(uid, "new")
    assert not at.exception and repo.list(uid) == []
    at.run()
    button(at, "새 대화").click().run()
    assert repo.list(uid) == [] and button(at, "이 대화 지우기").disabled
    at.selectbox[0].select("A-1").run()
    button(at, "대상 변경").click().run()
    assert any("A-1" in h.value for h in at.subheader) and repo.list(uid) == []
    question = "첫 질문의 앞 스무 글자만 제목으로 저장해야 합니다"
    at.chat_input[0].set_value(question).run()
    assert not at.exception
    rows = repo.list(uid)
    assert len(rows) == 1 and rows[0]["title"] == question[:20]
    assert rows[0]["subject"].set_num == "A-1"
    at.chat_input[0].set_value("두 번째 질문").run()
    assert repo.list(uid)[0]["title"] == question[:20]
    labels = [b.label for b in at.sidebar.button if b.label.startswith(question[:20])]
    expected_time = datetime.fromisoformat(repo.list(uid)[0]["updated_at"]).astimezone().strftime("%m/%d %H:%M")
    assert labels == [f"{question[:20]} · {expected_time}"]
    button(at, "새 대화").click().run()
    assert len(repo.list(uid)) == 1 and not at.chat_message
    at.run()
    assert not at.chat_message


def test_retry_keeps_request_id_and_hides_error(ui, monkeypatch):
    app, _, _, calls, _ = ui
    original = chat.chat_turn_v2
    attempts = []
    def flaky(*args, **kwargs):
        attempts.append(kwargs["request_id"])
        if len(attempts) == 1:
            raise RuntimeError("SECRET PROMPT TRACEBACK")
        return original(*args, **kwargs)
    monkeypatch.setattr(chat, "chat_turn_v2", flaky)
    at = app()
    at.chat_input[0].set_value("질문").run()
    assert not at.exception
    assert all("SECRET" not in str(e.value) for e in at.info)
    button(at, "같은 요청 다시 시도").click().run()
    assert not at.exception and len(calls) == 1 and attempts[0] == attempts[1]


def test_rag_resolved_question_keeps_original_storage(session):
    uid, cid, repo, deps, calls = session
    # 운영 데이터 없이도 명시된 회귀 사례의 세트 이름을 고정한다.
    with sqlite3.connect(deps.settings["paths"]["sqlite"]) as db:
        db.execute("UPDATE sets SET name='Han Solo', set_num='75535-1' WHERE set_num='A-1'")
        db.execute("UPDATE set_scope SET set_num='75535-1' WHERE set_num='A-1'")
    deps.conn.execute("UPDATE sets SET name='Han Solo', set_num='75535-1' WHERE set_num='A-1'")
    deps.conn.execute("UPDATE set_scope SET set_num='75535-1' WHERE set_num='A-1'")
    repo.save_turn_with_state(uid, cid, question="추천", response={}, kind="answer", subjects=[],
        state=DialogueState(last_list=ShownList("recommend", None, ("75535-1",))), expected_version=0, request_id=None)
    deps.model = model("narrative")
    result = send(session, "첫 번째 건 어떤 기체야?")
    assert "75535-1 Han Solo" in deps.ask_fn.call_args.args[1]
    assert calls.call_args.args[0] == "첫 번째 건 어떤 기체야?"
    assert result.question == "첫 번째 건 어떤 기체야?"


def test_expired_running_requests_retry_and_reset_clock(session):
    uid, cid, repo, deps, calls = session
    chat.reserve_chat_request(uid, cid, "A-1 하나 등록해줘", "timeout")
    with pytest.raises(chat.Busy):
        chat.reserve_chat_request(uid, cid, "A-1 하나 등록해줘", "timeout")
    expired = (datetime.now(timezone.utc) - timedelta(seconds=deps.settings["chat"]["request_timeout_sec"] + 1)).isoformat()
    with sqlite3.connect(repo.path) as db:
        db.execute("UPDATE chat_requests SET created_at=?", (expired,))
    job = chat.reserve_chat_request(uid, cid, "A-1 하나 등록해줘", "timeout")
    with pytest.raises(chat.Busy):
        chat.reserve_chat_request(uid, cid, "A-1 하나 등록해줘", "timeout")
    result = chat.execute_chat_request(job)
    assert send(session, "A-1 하나 등록해줘", "timeout") == result
    assert users.load_collection(uid)["version"] == 1


@pytest.mark.parametrize("word,pair,expected", [("팔콘", "을/를", "을"), ("레고", "을/를", "를"),
    ("부품", "이/가", "이"), ("세트", "이/가", "가"), ("팔콘", "은/는", "은"), ("레고", "은/는", "는"),
    ("Fighter", "을/를", "를"), ("Falcon", "을/를", "을"), ("Star Destroyer", "은/는", "는"),
    ("X-wing", "이/가", "이"), ('"Falcon"', "을/를", "을"), ("75301", "을/를", "을")])
def test_korean_particles(word, pair, expected):
    assert particle(word, pair) == expected


def test_unsaved_answer_visible_across_rerun_and_safe(ui, monkeypatch):
    app, _, _, _, _ = ui
    from rag_service.services.conversations import Turn
    def unsaved(*args, **kwargs):
        return Turn(0, args[2], dict(answer="보관할 답변", cards=[], tables=[], items=[],
            warnings=["답변을 저장하지 못했습니다."], timings={}), "2026-10-23")
    monkeypatch.setattr(chat, "chat_turn_v2", unsaved)
    at = app()
    at.chat_input[0].set_value("질문").run()
    at.run()
    assert not at.exception and len(at.chat_message) == 2
    assert any("보관할 답변" == m.value for m in at.markdown)
    assert any("저장하지 못했습니다" in c.value for c in at.caption)
    assert button(at, "같은 요청 다시 시도")


def test_expiry_is_scoped_to_conversation_and_allows_new_id(session):
    uid, cid, repo, deps, calls = session
    other = repo.create(uid)
    chat.reserve_chat_request(uid, cid, "질문", "old")
    chat.reserve_chat_request(uid, other, "질문", "other")
    expired = (datetime.now(timezone.utc) - timedelta(seconds=301)).isoformat()
    with sqlite3.connect(repo.path) as db:
        db.execute("UPDATE chat_requests SET created_at=? WHERE request_id='old'", (expired,))
    chat.reserve_chat_request(uid, cid, "다른 질문", "new")
    with sqlite3.connect(repo.path) as db:
        assert dict(db.execute("SELECT request_id, status FROM chat_requests")) == {
            "old": "failed", "other": "running", "new": "running"}
