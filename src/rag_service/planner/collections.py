"""참조 데이터와 분리된 사용자 컬렉션 저장소."""

import re
import sqlite3
from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from rag_service.config import load_settings


class CollectionConflict(Exception):
    """최신 컬렉션을 다시 불러와야 하는 충돌."""


@dataclass(frozen=True)
class CollectionChange:
    set_num: str
    op: str
    qty_before: int
    qty_after: int
    applied: bool
    replayed: bool
    needs_confirm: bool
    version: int


def validate_quantity(qty: int, maximum: int) -> None:
    if type(qty) is not int or not 1 <= qty <= maximum:
        raise ValueError(f"세트 수량은 1~{maximum} 사이의 정수여야 합니다.")


def _validate_number(number: str) -> None:
    if not isinstance(number, str) or not re.fullmatch(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*-[1-9][0-9]*", number):
        raise ValueError("정규화된 세트 번호 형식을 확인하세요.")


def _validate_cid(cid: str) -> None:
    if not isinstance(cid, str) or not re.fullmatch(r"[0-9a-fA-F]{32}", cid):
        raise ValueError("사용자 ID 형식을 확인하세요.")


class CollectionStore:
    """작업마다 연결을 닫고 전체 교체는 한 트랜잭션으로 수행한다."""

    def __init__(self, user_db_path: Path):
        self.path = Path(user_db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        schema = Path(__file__).resolve().parents[1] / "db/user_schema.sql"
        with closing(self._connect()) as conn, conn:
            # 새 테이블만 최신 열로 생성한다. 기존 DB의 ALTER는 명시적 migrate가 맡는다.
            conn.execute("CREATE TABLE IF NOT EXISTS collections "
                         "(id TEXT PRIMARY KEY, version INTEGER NOT NULL DEFAULT 0)")
            conn.executescript(schema.read_text(encoding="utf-8"))

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @staticmethod
    def _require_collection(conn: sqlite3.Connection, cid: str) -> None:
        if not conn.execute("SELECT 1 FROM collections WHERE id = ?", (cid,)).fetchone():
            raise KeyError("컬렉션을 찾을 수 없습니다. 컬렉션을 먼저 생성하세요.")

    def ensure(self, cid: str) -> None:
        """지정 ID의 컬렉션을 멱등 생성하며 기존 내용은 보존한다."""
        _validate_cid(cid)
        with closing(self._connect()) as conn, conn:
            conn.execute("INSERT OR IGNORE INTO collections(id) VALUES (?)", (cid,))

    def create(self) -> str:
        """새 컬렉션의 uuid4 hex를 반환한다."""
        cid = uuid4().hex
        with closing(self._connect()) as conn, conn:
            conn.execute("INSERT INTO collections(id) VALUES (?)", (cid,))
        return cid

    def set_sets(self, cid: str, sets: list[tuple[str, int]], expected_version: int | None = None,
                 *, ensure: bool = False) -> None:
        """목록을 교체한다. 중복 세트 번호의 수량은 합산한다."""
        quantities: Counter[str] = Counter()
        maximum = load_settings()["collection"]["max_qty"]
        if expected_version is not None and (type(expected_version) is not int or expected_version < 0):
            raise ValueError("컬렉션 버전은 0 이상의 정수여야 합니다.")
        if ensure:
            _validate_cid(cid)
        for number, qty in sets:
            _validate_number(number)
            validate_quantity(qty, maximum)
            quantities[number] += qty
            validate_quantity(quantities[number], maximum)
        self._prepare_change()
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            if ensure:
                conn.execute("INSERT OR IGNORE INTO collections(id) VALUES (?)", (cid,))
            self._require_collection(conn, cid)
            version = self._version(conn, cid, writing=True)
            if expected_version is not None and version != expected_version:
                raise CollectionConflict("컬렉션이 변경되었습니다. 최신 목록을 다시 불러오세요.")
            before = conn.execute("SELECT COALESCE(SUM(qty), 0) FROM collection_sets WHERE collection_id=?",
                                  (cid,)).fetchone()[0]
            conn.execute("DELETE FROM collection_sets WHERE collection_id = ?", (cid,))
            conn.executemany("INSERT INTO collection_sets VALUES (?, ?, ?)",
                             [(cid, number, qty) for number, qty in quantities.items()])
            self._record_change(conn, cid, None, "", "replace", before, sum(quantities.values()))

    def _prepare_change(self) -> None:
        """이전 UI가 만든 DB도 첫 저장 전에 기존 백업 이관 경로를 거친다."""
        with closing(self._connect()) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(collections)")}
        if "version" not in columns:
            from rag_service.services.migrations import migrate

            migrate(self.path)

    @staticmethod
    def _version(conn: sqlite3.Connection, cid: str, *, writing: bool = False) -> int:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(collections)")}
        if "version" not in columns:
            if writing:
                from rag_service.services.migrations import MigrationError

                raise MigrationError("컬렉션 저장소 이관이 필요합니다. 백업 후 migrate(path)를 실행하세요.")
            return 0
        return conn.execute("SELECT version FROM collections WHERE id=?", (cid,)).fetchone()[0]

    @staticmethod
    def _record_change(conn: sqlite3.Connection, cid: str, request_id: str | None,
                       number: str, op: str, before: int, after: int) -> None:
        conn.execute(
            "INSERT INTO collection_events(collection_id, request_id, set_num, op, qty_before, qty_after, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (cid, request_id, number, op, before, after, datetime.now(timezone.utc).isoformat()),
        )
        conn.execute("UPDATE collections SET version=version+1 WHERE id=?", (cid,))

    def apply_change(self, cid: str, set_num: str, op: str, qty: int | None,
                     request_id: str, *, statement: bool = False, confirm: bool = False,
                     max_qty: int | None = None) -> CollectionChange:
        """조회·확인 판단·변경·이벤트 기록을 같은 쓰기 잠금 안에서 수행한다."""
        _validate_cid(cid)
        _validate_number(set_num)
        if op not in ("set", "add", "remove"):
            raise ValueError("등록·추가·삭제 중 하나의 작업을 지정하세요.")
        if not isinstance(request_id, str) or not request_id.strip():
            raise ValueError("요청 ID를 확인하세요.")
        maximum = load_settings()["collection"]["max_qty"] if max_qty is None else max_qty
        if op != "remove" or qty is not None:
            validate_quantity(qty, maximum)
        self._prepare_change()
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            event = conn.execute(
                "SELECT id, collection_id, set_num, op, qty_before, qty_after "
                "FROM collection_events WHERE request_id=?", (request_id,),
            ).fetchone()
            if event:
                event_id, owner, number, saved_op, before, after = event
                if owner != cid:
                    raise CollectionConflict("이미 사용된 요청 ID입니다. 새 요청 ID로 다시 시도하세요.")
                # T-021 스키마를 유지하면서 이후 이벤트 수로 당시 버전을 복원한다.
                later = conn.execute("SELECT COUNT(*) FROM collection_events WHERE collection_id=? AND id>?",
                                     (cid, event_id)).fetchone()[0]
                version = self._version(conn, cid, writing=True) - later
                return CollectionChange(number, saved_op, before, after, True, True, False, version)
            conn.execute("INSERT OR IGNORE INTO collections(id) VALUES (?)", (cid,))
            version = self._version(conn, cid, writing=True)
            row = conn.execute("SELECT qty FROM collection_sets WHERE collection_id=? AND set_num=?",
                               (cid, set_num)).fetchone()
            before = row[0] if row else 0
            after = 0 if op == "remove" else (before + qty if op == "add" else qty)
            if after:
                validate_quantity(after, maximum)
            if op == "set" and statement and before > after and not confirm:
                return CollectionChange(set_num, op, before, after, False, False, True, version)
            if after:
                conn.execute("INSERT INTO collection_sets(collection_id, set_num, qty) VALUES (?, ?, ?) "
                             "ON CONFLICT(collection_id, set_num) DO UPDATE SET qty=excluded.qty",
                             (cid, set_num, after))
            else:
                conn.execute("DELETE FROM collection_sets WHERE collection_id=? AND set_num=?", (cid, set_num))
            self._record_change(conn, cid, request_id, set_num, op, before, after)
            return CollectionChange(set_num, op, before, after, True, False, False, version + 1)

    def add_adjustment(self, cid: str, part_num: str, color_id: int, delta: int) -> None:
        """보정 이력을 추가한다. 존재 여부 검증은 서비스 계층의 책임이다."""
        if not isinstance(part_num, str) or not part_num or any(c.isspace() for c in part_num):
            raise ValueError("부품 번호 형식을 확인하세요.")
        if type(color_id) is not int or type(delta) is not int:
            raise ValueError("색상과 보정 수량은 정수여야 합니다.")
        with closing(self._connect()) as conn, conn:
            self._require_collection(conn, cid)
            conn.execute(
                "INSERT INTO collection_adjustments(collection_id, part_num, color_id, delta) VALUES (?, ?, ?, ?)",
                (cid, part_num, color_id, delta),
            )

    def get(self, cid: str) -> dict:
        """세트는 번호순, 보정 이력은 등록순으로 반환한다."""
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN")
            self._require_collection(conn, cid)
            version = self._version(conn, cid)
            sets = conn.execute("SELECT set_num, qty FROM collection_sets WHERE collection_id = ? ORDER BY set_num", (cid,)).fetchall()
            adjustments = conn.execute(
                "SELECT part_num, color_id, delta FROM collection_adjustments WHERE collection_id = ? ORDER BY id", (cid,),
            ).fetchall()
        return {"id": cid, "sets": sets, "adjustments": adjustments, "version": version}
