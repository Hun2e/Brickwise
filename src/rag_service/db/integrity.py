"""CSV 적재와 부품 집계의 무결성 진단."""

import sqlite3
from dataclasses import dataclass

from rag_service.data_prep.rebrickable_download import TABLES


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str


def _reference_check(
    conn: sqlite3.Connection, column: str, parent: str, key: str, *, advisory: bool = False,
) -> CheckResult:
    query = f"""
        FROM inventory_parts p LEFT JOIN {parent} r ON p.{column} = r.{key}
        WHERE r.{key} IS NULL
    """
    count = conn.execute("SELECT COUNT(*) " + query).fetchone()[0]
    examples = [tuple(row) for row in conn.execute(
        f"SELECT p.inventory_id, p.part_num, p.color_id {query} LIMIT 5"
    )]
    return CheckResult(
        f"fk_inventory_parts_{column}", advisory or count == 0,
        f"violations={count}; examples={examples}" + ("; advisory only" if advisory else ""),
    )


def run_checks(
    conn: sqlite3.Connection, csv_counts: dict[str, int] | None = None,
) -> list[CheckResult]:
    """모든 검사 결과를 반환한다. 부품/색상 참조 누락은 정보로만 보고한다."""
    checks = []
    for table in TABLES:
        actual = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        expected = csv_counts.get(table) if csv_counts is not None else None
        checks.append(CheckResult(
            f"row_count_{table}", csv_counts is None or actual == expected,
            f"db={actual}; csv={expected if expected is not None else 'not provided'}",
        ))
    checks.extend([
        _reference_check(conn, "inventory_id", "inventories", "id"),
        _reference_check(conn, "part_num", "parts", "part_num", advisory=True),
        _reference_check(conn, "color_id", "colors", "id", advisory=True),
    ])
    duplicates = conn.execute("""
        SELECT COUNT(*) FROM (
            SELECT set_num, part_num, color_id FROM set_parts_flat
            GROUP BY set_num, part_num, color_id HAVING COUNT(*) > 1
        )
    """).fetchone()[0]
    checks.append(CheckResult("set_parts_flat_pk", duplicates == 0, f"duplicates={duplicates}"))
    counts = dict(conn.execute("SELECT scope, COUNT(*) FROM set_scope GROUP BY scope"))
    core = counts.get("core", 0)
    checks.append(CheckResult("core_count", True, f"count={core}"))
    mismatches = [tuple(row) for row in conn.execute("""
        SELECT s.set_num, s.num_parts, COALESCE(SUM(p.quantity), 0) AS actual
        FROM set_scope c JOIN sets s USING (set_num)
        LEFT JOIN set_parts_flat p USING (set_num)
        WHERE c.scope = 'core' GROUP BY s.set_num, s.num_parts
        HAVING s.num_parts != actual ORDER BY s.set_num
    """)]
    checks.append(CheckResult(
        "core_num_parts", not mismatches,
        f"matched={core - len(mismatches)}/{core}; mismatches(set_num, expected, actual)={mismatches}",
    ))
    checks.append(CheckResult("compare_only_count", True, f"count={counts.get('compare_only', 0)}"))
    return checks
