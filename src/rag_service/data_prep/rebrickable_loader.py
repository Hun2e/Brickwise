"""gzip CSV 스트리밍 적재 및 전체 세트의 결정적 부품 집계."""

import csv
import gzip
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import perf_counter
from typing import Iterator

from rag_service.config import load_settings, resolve_path
from rag_service.data_prep.rebrickable_download import TABLES
from rag_service.db.connection import connect


@dataclass
class LoadReport:
    row_counts: dict[str, int]
    csv_counts: dict[str, int]
    seconds: float
    db_bytes: int


def _rows(path: Path, columns: list[tuple[str, str]]) -> Iterator[tuple]:
    with gzip.open(path, "rt", encoding="utf-8-sig", newline="") as stream:
        reader = csv.reader(stream)
        expected = [name for name, _ in columns]
        if next(reader, None) != expected:
            raise ValueError(f"{path.name}: CSV 컬럼이 스키마와 다릅니다.")
        for row in reader:
            if len(row) != len(columns):
                raise ValueError(f"{path.name}:{reader.line_num}: CSV 열 수가 다릅니다.")
            values = []
            for value, (name, kind) in zip(row, columns):
                if name in ("is_trans", "is_spare"):
                    normalized = value.strip().lower()
                    if normalized not in ("true", "false", "t", "f", "0", "1"):
                        raise ValueError(f"{path.name}:{reader.line_num}: 불리언 형식 오류")
                    values.append(int(normalized in ("true", "t", "1")))
                elif kind == "INTEGER":
                    values.append(int(value) if value.strip() else None)
                else:
                    values.append(value)
            yield tuple(values)


def _load_table(conn: sqlite3.Connection, table: str, path: Path, batch_size: int) -> int:
    columns = [(row[1], row[2]) for row in conn.execute(f"PRAGMA table_info({table})")]
    placeholders = ",".join("?" for _ in columns)
    count = 0
    rows = _rows(path, columns)
    try:
        while batch := list(islice(rows, batch_size)):
            conn.executemany(f"INSERT INTO {table} VALUES ({placeholders})", batch)
            count += len(batch)
    finally:
        rows.close()
    return count


def load(csv_dir: Path, sqlite_path: Path) -> LoadReport:
    """임시 DB에 12종을 적재한 후 교체한다. 실패하면 기존 DB를 보존한다."""
    started = perf_counter()
    csv_dir, sqlite_path = resolve_path(csv_dir), resolve_path(sqlite_path)
    batch_size = load_settings()["rebrickable"]["batch_size"]
    if not isinstance(batch_size, int) or batch_size <= 0:
        raise ValueError("rebrickable.batch_size는 양의 정수여야 합니다.")
    for table in TABLES:
        if not (csv_dir / f"{table}.csv.gz").is_file():
            raise FileNotFoundError(f"{table}.csv.gz 파일이 없습니다. 원본 12종을 준비하세요.")
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(prefix=f"{sqlite_path.name}.", suffix=".tmp", dir=sqlite_path.parent, delete=False) as file:
        temporary = Path(file.name)
    try:
        with closing(connect(temporary)) as conn:
            schema = Path(__file__).resolve().parents[1] / "db" / "schema.sql"
            conn.executescript(schema.read_text(encoding="utf-8"))
            with conn:
                csv_counts = {
                    table: _load_table(conn, table, csv_dir / f"{table}.csv.gz", batch_size)
                    for table in TABLES
                }
            row_counts = {
                table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in TABLES
            }
        temporary.replace(sqlite_path)
    finally:
        temporary.unlink(missing_ok=True)
    return LoadReport(row_counts, csv_counts, perf_counter() - started, sqlite_path.stat().st_size)


def _build_root_themes(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM set_root_theme")
    conn.execute("""
        WITH RECURSIVE roots(id, root_id, root_name) AS (
            SELECT id, id, name FROM themes WHERE parent_id IS NULL
            UNION ALL
            SELECT child.id, roots.root_id, roots.root_name
            FROM themes child JOIN roots ON child.parent_id = roots.id
        )
        INSERT INTO set_root_theme
        SELECT s.set_num, r.root_id, r.root_name, t.name
        FROM sets s JOIN themes t ON t.id = s.theme_id JOIN roots r ON r.id = t.id
    """)
    missing = conn.execute("""
        SELECT COUNT(*) FROM sets s LEFT JOIN set_root_theme r USING (set_num)
        WHERE r.set_num IS NULL
    """).fetchone()[0]
    if missing:
        raise ValueError(f"루트 테마를 찾을 수 없는 세트 {missing}개: 테마 누락/순환을 확인하세요.")


def _build_parts(conn: sqlite3.Connection) -> None:
    # 실데이터에서 is_spare 자동 인덱스를 선택하면 미니피그마다 대량 재검색한다.
    # 아래 JOIN은 인벤토리별 조회용 인덱스를 명시해 이 실행 계획을 방지한다.
    conn.execute("DROP TABLE IF EXISTS temp.latest_inventories")
    conn.execute("""
        CREATE TEMP TABLE latest_inventories AS
        SELECT id, set_num FROM (
            SELECT id, set_num, ROW_NUMBER() OVER (
                PARTITION BY set_num ORDER BY version DESC, id DESC
            ) AS rank FROM inventories
        ) WHERE rank = 1
    """)
    conn.execute("CREATE UNIQUE INDEX temp.idx_latest_set ON latest_inventories(set_num)")
    conn.execute("DELETE FROM set_parts_flat")
    conn.execute("""
        INSERT INTO set_parts_flat (set_num, part_num, color_id, quantity)
        SELECT set_num, part_num, color_id, SUM(quantity) FROM (
            SELECT s.set_num, p.part_num, p.color_id, p.quantity
            FROM sets s JOIN latest_inventories i USING (set_num)
            JOIN inventory_parts p INDEXED BY idx_inventory_parts_inventory
                ON p.inventory_id = i.id WHERE p.is_spare = 0
            UNION ALL
            SELECT s.set_num, p.part_num, p.color_id, p.quantity * m.quantity
            FROM sets s JOIN latest_inventories i USING (set_num)
            JOIN inventory_minifigs m ON m.inventory_id = i.id
            JOIN latest_inventories fig ON fig.set_num = m.fig_num
            JOIN inventory_parts p INDEXED BY idx_inventory_parts_inventory
                ON p.inventory_id = fig.id WHERE p.is_spare = 0
        ) GROUP BY set_num, part_num, color_id
    """)
    conn.execute("DROP TABLE temp.latest_inventories")


def build_derived(conn: sqlite3.Connection) -> dict[str, int]:
    """최상위 테마와 최신 인벤토리의 미니피그 포함 부품 합계를 재생성한다."""
    with conn:
        _build_root_themes(conn)
        _build_parts(conn)
    return {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("set_root_theme", "set_parts_flat")
    }
