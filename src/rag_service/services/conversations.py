"""사용자·대화 키별 전체 응답을 SQLite에 보존한다."""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from rag_service.config import load_settings

from rag_service.services.users import collection_id_for, user_db_path
from rag_service.services.dialogue_state import DialogueState, validate_set_num
from rag_service.services.migrations import MigrationError, migrate, migration_needed

ThreadKey = str


@dataclass
class Turn:
    id: int
    question: str
    response: dict
    created_at: str
    conversation_id: str | None = None
    request_id: str | None = None
    kind: str = "answer"
    state_version: int | None = None
    subjects: list[tuple[str, str]] = field(default_factory=list)

    @property
    def comparison_label(self) -> str | None:
        numbers = [number for number, role in self.subjects if role == "compared"]
        return "비교: " + "·".join(numbers) if numbers else None


def validate_thread(thread: ThreadKey) -> None:
    if not isinstance(thread, str) or not (
        thread in ("explore", "collection")
        or re.fullmatch(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*-[1-9][0-9]*", thread)
    ):
        raise ValueError("대화의 세트 번호 또는 탭을 확인하세요.")


class ConversationStore:
    def __init__(self, user_db_path: Path):
        self.path = Path(user_db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        schema = Path(__file__).resolve().parents[1] / "db/user_schema.sql"
        with closing(self._connect()) as conn, conn:
            conn.executescript(schema.read_text(encoding="utf-8"))

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def append(self, uid: str, thread: ThreadKey, question: str, response: dict) -> Turn:
        collection_id_for(uid)
        validate_thread(thread)
        payload = json.dumps(response, ensure_ascii=False, allow_nan=False)
        created_at = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as conn, conn:
            cursor = conn.execute(
                "INSERT INTO conversation_turns(uid, thread, question, response, created_at) VALUES (?, ?, ?, ?, ?)",
                (uid, thread, question, payload, created_at),
            )
            turn_id = cursor.lastrowid
        return Turn(turn_id, question, json.loads(payload), created_at)

    def turns(self, uid: str, thread: ThreadKey, limit: int | None = None) -> list[Turn]:
        collection_id_for(uid)
        validate_thread(thread)
        if limit is not None and (type(limit) is not int or limit < 0):
            raise ValueError("대화 조회 개수는 0 이상의 정수여야 합니다.")
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT id, question, response, created_at FROM conversation_turns "
                "WHERE uid=? AND thread=? ORDER BY id DESC LIMIT ?",
                (uid, thread, -1 if limit is None else limit),
            ).fetchall()
        return [Turn(i, q, json.loads(r), at) for i, q, r, at in reversed(rows)]

    def threads(self, uid: str) -> list[dict]:
        collection_id_for(uid)
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT thread, COUNT(*), MAX(created_at) FROM conversation_turns WHERE uid=? "
                "GROUP BY thread ORDER BY MAX(id) DESC", (uid,),
            ).fetchall()
        return [dict(zip(("thread", "turns", "updated_at"), row)) for row in rows]

    def clear(self, uid: str, thread: ThreadKey) -> int:
        collection_id_for(uid)
        validate_thread(thread)
        with closing(self._connect()) as conn, conn:
            return conn.execute("DELETE FROM conversation_turns WHERE uid=? AND thread=?", (uid, thread)).rowcount


def history_for(turns: list[Turn], n: int) -> list[dict]:
    """대화 원문은 저장하고 모델에는 요청된 최근 턴만 전달한다."""
    return [{"question": turn.question, "answer": turn.response.get("answer", "")}
            for turn in (turns[-n:] if n > 0 else [])]


def conversation_store() -> ConversationStore:
    return ConversationStore(user_db_path())


class StateConflict(Exception):
    """다른 요청이 상태를 먼저 변경했으므로 다시 읽어야 한다."""


class ConversationRepo:
    """통합 타임라인과 대상별 기록을 분리하는 사용자 소유 저장소.

    새 DB만 초기화한다. 기존 DB는 호출자가 migrate(path)로 먼저 이관한다.
    load_state는 (DialogueState, version)을 반환하고 delete는 삭제한 턴 수를 반환한다.
    """

    def __init__(self, user_db_path: str | Path):
        self.path = Path(user_db_path).resolve()
        if not self.path.exists():
            migrate(self.path)
        elif migration_needed(self.path):
            raise MigrationError("대화 저장소 이관이 필요합니다. 백업 후 migrate(path)를 실행하세요.")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @staticmethod
    def _require(conn: sqlite3.Connection, uid: str, cid: str) -> sqlite3.Row:
        collection_id_for(uid)
        row = conn.execute("SELECT * FROM conversations WHERE uid=? AND id=?", (uid, cid)).fetchone()
        if row is None:
            raise KeyError("대화를 찾을 수 없습니다.")
        return row

    @staticmethod
    def _state(conn: sqlite3.Connection, cid: str) -> tuple[DialogueState, int]:
        row = conn.execute("SELECT state_json, version FROM conversation_state WHERE conversation_id=?", (cid,)).fetchone()
        if row is None:
            raise MigrationError("대화 상태가 없습니다. DB와 백업을 확인하세요.")
        return DialogueState.from_json(row[0]), row[1]

    @staticmethod
    def _turns(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> list[Turn]:
        turns = []
        for row in rows:
            subjects = [tuple(value) for value in conn.execute(
                "SELECT set_num, role FROM turn_subjects WHERE turn_id=? ORDER BY rowid", (row["id"],))]
            turns.append(Turn(row["id"], row["question"], json.loads(row["response"]), row["created_at"],
                              row["conversation_id"], row["request_id"], row["kind"], row["state_version"], subjects))
        return turns

    def create(self, uid: str, title: str | None = None) -> str:
        collection_id_for(uid)
        if title is not None and (not isinstance(title, str) or not title.strip()):
            raise ValueError("대화 제목을 확인하세요.")
        cid, now = uuid4().hex, datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as conn, conn:
            conn.execute("INSERT INTO conversations(id, uid, title, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                         (cid, uid, title or "새 대화", now, now))
            conn.execute("INSERT INTO conversation_state VALUES (?, ?, 0, ?)", (cid, DialogueState().to_json(), now))
        return cid

    def list(self, uid: str) -> list[dict]:
        collection_id_for(uid)
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT c.id, c.title, c.updated_at, s.state_json, "
                "(SELECT COUNT(*) FROM conversation_turns t WHERE t.conversation_id=c.id AND t.uid=c.uid) AS turns "
                "FROM conversations c JOIN conversation_state s ON s.conversation_id=c.id "
                "WHERE c.uid=? ORDER BY c.updated_at DESC, c.id", (uid,),
            ).fetchall()
        return [{"id": row["id"], "title": row["title"], "updated_at": row["updated_at"],
                 "subject": DialogueState.from_json(row["state_json"]).subject, "turns": row["turns"]} for row in rows]

    def get(self, uid: str, cid: str) -> dict:
        with closing(self._connect()) as conn:
            conn.execute("BEGIN")
            conversation = self._require(conn, uid, cid)
            state, version = self._state(conn, cid)
            rows = conn.execute("SELECT * FROM conversation_turns WHERE uid=? AND conversation_id=? "
                                "ORDER BY created_at, id", (uid, cid)).fetchall()
            return {"id": cid, "title": conversation["title"], "turns": self._turns(conn, rows),
                    "state": state, "version": version}

    def load_state(self, uid: str, cid: str) -> tuple[DialogueState, int]:
        with closing(self._connect()) as conn:
            conn.execute("BEGIN")
            self._require(conn, uid, cid)
            return self._state(conn, cid)

    def save_turn_with_state(
        self, uid: str, cid: str, *, question: str, response: dict, kind: str,
        subjects: list[tuple[str, str]], state: DialogueState, expected_version: int,
        request_id: str | None,
    ) -> Turn:
        collection_id_for(uid)
        if type(expected_version) is not int or expected_version < 0:
            raise ValueError("상태 버전은 0 이상의 정수여야 합니다.")
        if not isinstance(question, str) or not isinstance(response, dict) or not isinstance(kind, str) or not kind:
            raise ValueError("질문과 응답·턴 종류를 확인하세요.")
        if request_id is not None and (not isinstance(request_id, str) or not request_id.strip()):
            raise ValueError("요청 ID를 확인하세요.")
        links = list(dict.fromkeys(subjects))
        for number, role in links:
            validate_set_num(number)
            if role not in ("primary", "compared", "listed"):
                raise ValueError("세트 연결 역할을 확인하세요.")
        primary = [number for number, role in links if role == "primary"]
        compared = [number for number, role in links if role == "compared"]
        # 비교 턴은 양쪽 모두 compared로 기록해야 다른 primary 턴이 섞이지 않는다.
        if len(primary) > 1 or (primary and compared):
            raise ValueError("대표 대상은 하나이며 비교 대상과 함께 지정할 수 없습니다.")
        payload, state_json = json.dumps(response, ensure_ascii=False, allow_nan=False), state.to_json()
        thread = primary[0] if primary else (state.subject.set_num if state.subject else "general")
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            self._require(conn, uid, cid)
            now = datetime.now(timezone.utc).isoformat()
            updated = conn.execute(
                "UPDATE conversation_state SET state_json=?, version=version+1, updated_at=? "
                "WHERE conversation_id=? AND version=?", (state_json, now, cid, expected_version),
            ).rowcount
            if not updated:
                raise StateConflict("대화가 변경되었습니다. 최신 상태를 다시 불러오세요.")
            cursor = conn.execute(
                "INSERT INTO conversation_turns(uid, thread, question, response, created_at, conversation_id, "
                "request_id, kind, state_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (uid, thread, question, payload, now, cid, request_id, kind, expected_version + 1),
            )
            turn_id = cursor.lastrowid
            conn.executemany("INSERT INTO turn_subjects(turn_id, set_num, role) VALUES (?, ?, ?)",
                             [(turn_id, number, role) for number, role in links])
            conn.execute(
                "UPDATE conversations SET updated_at=?, title=CASE WHEN title='새 대화' AND "
                "(SELECT COUNT(*) FROM conversation_turns WHERE conversation_id=?)=1 "
                "THEN ? ELSE title END WHERE id=?",
                (now, cid, question.strip()[:load_settings()["chat"]["title_chars"]], cid),
            )
        return Turn(turn_id, question, json.loads(payload), now, cid, request_id, kind, expected_version + 1, links)

    def set_history(self, uid: str, set_num: str) -> list[Turn]:
        collection_id_for(uid)
        validate_set_num(set_num)
        with closing(self._connect()) as conn:
            conn.execute("BEGIN")
            rows = conn.execute(
                "SELECT t.* FROM conversation_turns t JOIN conversations c ON c.id=t.conversation_id "
                "WHERE t.uid=? AND c.uid=? AND EXISTS (SELECT 1 FROM turn_subjects s "
                "WHERE s.turn_id=t.id AND s.set_num=? AND s.role IN ('primary', 'compared')) "
                "ORDER BY t.created_at, t.id", (uid, uid, set_num),
            ).fetchall()
            return self._turns(conn, rows)

    def context_turns(self, uid: str, cid: str, subject: str | None, n: int) -> list[Turn]:
        if type(n) is not int or n < 0:
            raise ValueError("대화 조회 개수는 0 이상의 정수여야 합니다.")
        if subject is not None:
            validate_set_num(subject)
        with closing(self._connect()) as conn:
            conn.execute("BEGIN")
            self._require(conn, uid, cid)
            rows = conn.execute(
                "SELECT t.* FROM conversation_turns t WHERE uid=? AND conversation_id=? "
                "AND EXISTS (SELECT 1 FROM turn_subjects s WHERE s.turn_id=t.id AND s.set_num=? AND s.role='primary') "
                "ORDER BY t.created_at DESC, t.id DESC LIMIT ?", (uid, cid, subject, n),
            ).fetchall()
            return self._turns(conn, list(reversed(rows)))

    def delete(self, uid: str, cid: str) -> int:
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            self._require(conn, uid, cid)
            count = conn.execute("SELECT COUNT(*) FROM conversation_turns WHERE conversation_id=?", (cid,)).fetchone()[0]
            conn.execute("DELETE FROM conversations WHERE id=?", (cid,))
        return count
