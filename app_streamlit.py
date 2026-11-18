"""대화 ID와 공통 서비스로 구성한 Streamlit 화면."""

import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from rag_service.services.catalog import SetOption, catalog_options
from rag_service.services.chat import (
    chat_turn_v2, cited_cards, create_conversation, list_conversations, get_conversation,
    change_subject, delete_conversation, set_history, draft_conversation, Busy, StateConflict,
)
from rag_service.services.conversations import Turn
from rag_service.services.status import check_status, ui_settings
from rag_service.services.users import (
    load_collection, new_user_id, save_collection, valid_user_id, CollectionConflict,
)


def switch_conversation(cid: str) -> None:
    st.query_params["conv"] = cid
    st.session_state.pop("submission", None)
    st.session_state.pop("stage_log", None)
    st.session_state.pop("draft_subject", None)


def new_conversation(uid: str) -> None:
    switch_conversation("new")


def select_subject(uid: str, cid: str, number: str | None, version: int) -> None:
    try:
        if cid:
            change_subject(uid, cid, number, version)
        else:
            st.session_state.draft_subject = number
    except StateConflict:
        st.session_state.notice = "대상 상태가 변경됐어요. 최신 대화를 불러왔으니 다시 선택하세요."
    except Exception:
        st.session_state.notice = "대상을 바꾸지 못했습니다. 세트 번호와 저장소 상태를 확인하세요."


def queue_question(uid: str, cid: str, question: str, card_key: str | None = None) -> None:
    """제출 때만 ID를 발급하고 rerun·재시도·동일 카드 클릭에는 재사용한다."""
    if card_key and card_key in st.session_state.get("completed_cards", set()):
        st.session_state.notice = "이 카드의 보유 등록은 이미 완료됐어요."
        return
    previous = st.session_state.get("submission")
    if previous and previous["uid"] == uid and previous["cid"] == cid and previous["question"] == question:
        previous["ready"] = True
        return
    ids = st.session_state.setdefault("card_requests", {})
    request_id = ids.setdefault(card_key, uuid4().hex) if card_key else uuid4().hex
    st.session_state.submission = dict(uid=uid, cid=cid, question=question, request_id=request_id,
                                       card_key=card_key, ready=True)


def retry_question() -> None:
    st.session_state.submission["ready"] = True


def show_response(turn: Turn, uid: str, cid: str, version: int, key: str, enabled: bool) -> None:
    response = turn.response
    st.markdown(response.get("answer") or response.get("needs_input") or "확인되지 않음")
    if response.get("warnings"):
        st.caption(" · ".join(response["warnings"]))
    action = response.get("action")
    if action:
        st.text(f"{action['set_num']}: 보유 {action['qty_before']}개 → {action['qty_after']}개")
    for item in response.get("items", []):
        number = item["set_num"]
        with st.container(border=True):
            st.badge(str(item["rank"]))
            st.text(f"{number} {item['name']}")
            st.caption(f"{item['year']}년 · 부품 {item['num_parts']:,}개")
            if item.get("missing") is not None:
                st.caption(f"부족 {item['missing']:,}개 ({item['lots_missing']:,}종)")
            st.button("이 세트로 질문", key=f"subject:{key}:{number}", on_click=select_subject,
                      args=(uid, cid, number, version))
            card_key = f"{uid}:{cid}:{turn.id}:{number}"
            st.button("보유 등록", key=f"own:{key}:{number}", disabled=not enabled,
                      on_click=queue_question,
                      args=(uid, cid, f"{number} 하나 보유 목록에 등록해줘", card_key))
    if response.get("route") == "filter" and not response.get("items") and not response.get("needs_input"):
        st.info("조건에 맞는 세트가 없습니다. 조건을 바꾸어 다시 탐색하세요.")
    if response.get("tables"):
        with st.expander("상세 표"):
            for table in response["tables"]:
                st.caption(table["title"])
                st.dataframe([dict(zip(table["columns"], row)) for row in table["rows"]], hide_index=True)
    with st.expander("처리 정보"):
        st.caption(f"경로: {response.get('route', '')} · 소요 시간: {response.get('timings', {}).get('total_ms', 0)} ms")
    if not turn.id:
        st.caption("이 답변은 저장되지 않았습니다. 새로고침 전에 내용을 보관하세요.")
    cards = cited_cards(response)
    if not cards:
        st.info("답변에 인용된 근거가 없습니다")
    for card in cards:
        with st.expander(f"[근거 {card['n']}] {card['source_type']} · {card['title']}"):
            st.badge(card["source_type"])
            st.text(card["title"])
            if urlsplit(card["url"]).scheme in ("https", "http"):
                st.link_button("원문 보기", card["url"])
            st.caption(f"라이선스: {card['license']}")
            st.text(card["text"])


def reload_collection(uid: str) -> None:
    try:
        st.session_state[f"collection:{uid}"] = load_collection(uid)
        st.session_state[f"editor_revision:{uid}"] = st.session_state.get(f"editor_revision:{uid}", 0) + 1
    except Exception:
        st.session_state.notice = "컬렉션을 읽지 못했습니다. 저장소 상태를 확인하세요."


def collection_editor(uid: str, options: dict[str, SetOption], enabled: bool) -> None:
    snapshot_key = f"collection:{uid}"
    if snapshot_key not in st.session_state:
        reload_collection(uid)
    collection = st.session_state.get(snapshot_key)
    if collection is None:
        return
    revision = st.session_state.get(f"editor_revision:{uid}", 0)
    with st.form(f"collection-form:{uid}"):
        edited = st.data_editor(
            {"세트": [n for n, _ in collection["sets"]] or [None],
             "수량": [q for _, q in collection["sets"]] or [None]},
            column_config={
                "세트": st.column_config.SelectboxColumn("세트", options=list(options), required=True),
                "수량": st.column_config.NumberColumn("수량", min_value=1, step=1, required=True),
            }, num_rows="dynamic", hide_index=True, disabled=not enabled, key=f"owned:{uid}:{revision}",
        )
        submitted = st.form_submit_button("컬렉션 저장", disabled=not enabled)
    for number, qty in collection["sets"]:
        st.caption(f"{options[number].label() if number in options else number} · {qty}개")
    st.button("컬렉션 다시 불러오기", on_click=reload_collection, args=(uid,), disabled=not enabled)
    if submitted:
        try:
            rows = [(n, int(q) if type(q) is float and q.is_integer() else q)
                    for n, q in zip(edited["세트"], edited["수량"]) if n is not None or q is not None]
            save_collection(uid, rows, expected_version=collection["version"])
        except CollectionConflict:
            st.error("보유 목록이 변경됐어요. 컬렉션을 다시 불러온 뒤 수정하세요.")
        except Exception:
            st.error("컬렉션을 저장하지 못했습니다. 세트 번호·정수 수량과 저장소 권한을 확인하세요.")
        else:
            reload_collection(uid)
            st.session_state.notice = "컬렉션을 저장했습니다."
            st.rerun()


def process_submission(uid: str, cid: str) -> None:
    job = st.session_state.get("submission")
    if not job or not job["ready"] or (job["uid"], job["cid"]) != (uid, cid):
        return
    job["ready"] = False
    stages = []
    with st.status("질문 처리 중", expanded=True) as progress:
        def stage(label: str, detail: str | None) -> None:
            text = label + (f": {detail}" if detail else "")
            if not stages or stages[-1] != text:
                stages.append(text)
                progress.write(text)
        try:
            turn = chat_turn_v2(uid, cid, job["question"], request_id=job["request_id"], on_stage=stage)
        except Busy:
            st.session_state.notice = "이전 질문을 처리 중입니다. 잠시 후 같은 요청으로 다시 시도하세요."
            progress.update(label="처리 대기", state="error")
            st.info(st.session_state.notice)
            return
        except Exception:
            st.session_state.notice = "질문을 처리하지 못했습니다. 입력과 저장소 상태를 확인하고 다시 시도하세요."
            progress.update(label="처리 실패", state="error")
            st.error(st.session_state.notice)
            return
    st.session_state.stage_log = stages
    transient = st.session_state.setdefault("unsaved", {}).setdefault((uid, cid), {})
    if turn.id:
        transient.pop(job["request_id"], None)
        st.session_state.pop("submission", None)
    else:
        transient[job["request_id"]] = turn
    if turn.response.get("action", {}).get("applied") and job["card_key"]:
        st.session_state.setdefault("completed_cards", set()).add(job["card_key"])
    if turn.response.get("action"):
        # 편집 중이던 스냅샷은 보존하여 오래된 저장을 version으로 감지한다.
        st.session_state.notice = "보유 목록이 변경됐어요. 편집하려면 컬렉션을 다시 불러오세요."
    st.rerun()


def main() -> None:
    cfg = ui_settings()
    st.set_page_config(page_title=cfg["title"], layout="wide")
    uid = st.query_params.get("uid", "")
    if not valid_user_id(uid):
        uid = new_user_id()
        st.query_params["uid"] = uid
    status = check_status()
    st.title(cfg["title"])
    st.caption(cfg["disclaimer"])
    st.badge("질문 가능" if status.can_chat else "준비 필요")
    st.button("새 대화", on_click=new_conversation, args=(uid,), disabled=not status.user_db_ok)
    try:
        options = {option.set_num: option for option in catalog_options()}
    except Exception:
        options = {}
    try:
        rows = list_conversations(uid)
        cid = st.query_params.get("conv", "")
        if cid == "new":
            cid = ""
        if "conv" not in st.query_params and rows:
            cid = rows[0]["id"]
            switch_conversation(cid)
        if cid and cid not in {row["id"] for row in rows}:
            cid = ""
            switch_conversation("new")
        current = get_conversation(uid, cid) if cid else draft_conversation(st.session_state.get("draft_subject"))
    except Exception:
        st.error("대화를 읽지 못했습니다. 사용자 DB 경로와 권한을 확인하세요.")
        st.chat_input("질문", disabled=True)
        return
    with st.sidebar:
        for message in status.messages:
            st.info(message)
        st.subheader("내 대화")
        for row in rows:
            local_time = datetime.fromisoformat(row["updated_at"]).astimezone().strftime("%m/%d %H:%M")
            st.button(f"{row['title']} · {local_time}", key=f"conv:{row['id']}",
                      on_click=switch_conversation, args=(row["id"],))
        if st.button("이 대화 지우기", disabled=not status.user_db_ok or not cid):
            try:
                delete_conversation(uid, cid)
                st.session_state.get("unsaved", {}).pop((uid, cid), None)
                new_conversation(uid)
                st.rerun()
            except Exception:
                st.error("대화를 지우지 못했습니다. 저장소 상태를 확인하세요.")
        with st.expander("내 컬렉션"):
            collection_editor(uid, options, status.db_ok and status.user_db_ok)
        st.subheader("세트별 기록")
        subject = current["state_summary"]["subject"]
        if subject:
            try:
                history = set_history(uid, subject)
                if not history:
                    st.caption("이 세트의 대화 기록이 없습니다.")
                for turn in history:
                    with st.expander(turn.question):
                        if turn.comparison_label:
                            st.badge(turn.comparison_label)
                        st.text(turn.response.get("answer", ""))
                        st.caption(turn.created_at)
            except Exception:
                st.info("세트별 기록을 읽지 못했습니다. 저장소 상태를 확인하세요.")
        else:
            st.caption("대상을 선택하면 세트별 기록을 볼 수 있어요.")
    summary = current["state_summary"]
    st.subheader(f"현재 대상: {subject} {summary['subject_name'] or ''}" if subject
                 else "대상 없음 — 바로 질문해도 돼요")
    with st.expander("변경"):
        selected = st.selectbox("번호 · 이름 · 별칭으로 세트 검색", list(options), index=None,
                                format_func=lambda n: options[n].label() + " · " + " / ".join(options[n].aliases))
        st.button("대상 변경", disabled=not selected, on_click=select_subject,
                  args=(uid, cid, selected, current["version"]))
    st.button("해제", disabled=not subject, on_click=select_subject, args=(uid, cid, None, current["version"]))
    if st.session_state.get("notice"):
        st.info(st.session_state.pop("notice"))
    if st.session_state.get("stage_log"):
        with st.status("처리 완료", state="complete", expanded=False) as progress:
            for text in st.session_state.stage_log:
                progress.write(text)
    turns = [*current["turns"], *st.session_state.get("unsaved", {}).get((uid, cid), {}).values()]
    for index, turn in enumerate(turns):
        with st.chat_message("user"):
            st.write(turn.question)
        with st.chat_message("assistant"):
            if cfg["risky_domain"]:
                st.caption(cfg["disclaimer"])
            show_response(turn, uid, cid, current["version"], f"{cid}:{index}", status.can_chat)
    pending = current["state"].pending
    if pending:
        st.info(summary["pending"])
        if pending.kind == "quantity":
            quantity = st.number_input("보유 수량", min_value=1, step=1, value=1)
            st.button("수량 보내기", on_click=queue_question, args=(uid, cid, f"{quantity}개"),
                      disabled=not status.can_chat)
        choices = [("네", "응"), ("아니요", "아니")] if pending.kind == "confirm" else (
            [(n, n) for n in pending.options] if pending.kind == "target" else [])
        for label, value in choices:
            st.button(label, key=f"pending:{value}", on_click=queue_question, args=(uid, cid, value), disabled=not status.can_chat)
    question = st.chat_input("찾고 싶은 세트나 궁금한 이야기를 질문하세요", disabled=not status.can_chat)
    if question:
        if not cid:
            try:
                cid = create_conversation(uid)
                switch_conversation(cid)
                if subject:
                    change_subject(uid, cid, subject, 0)
            except Exception:
                st.error("대화를 만들지 못했습니다. 저장소 상태를 확인하세요.")
                return
        queue_question(uid, cid, question)
    if status.can_chat:
        process_submission(uid, cid)
    if st.session_state.get("submission"):
        st.button("같은 요청 다시 시도", on_click=retry_question, disabled=not status.can_chat)


if __name__ == "__main__":
    main()
