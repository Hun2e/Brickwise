"""후보를 일괄 조회하고 부족 수량 순으로 추천한다."""

import sqlite3
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from rag_service.config import load_settings
from .inventory import rekey
from .matcher import _missing
from .sets import SET_SELECT, SetInfo


@dataclass(frozen=True)
class Recommendation:
    set_info: SetInfo
    total_missing: int
    lots_missing: int
    coverage: float
    need_total: int
    count_mismatch: bool


def recommend(
    conn: sqlite3.Connection, have: Counter[tuple[str, int]], owned_set_nums: set[str],
    mode: str | None = None, scope: Sequence[str] | None = None,
    year_min: int | None = None, year_max: int | None = None,
    parts_min: int | None = None, parts_max: int | None = None,
    top_n: int | None = None,
) -> list[Recommendation]:
    """기본값은 settings를 따르며 all은 범위 밖 세트도 포함한다."""
    if mode is None or scope is None or top_n is None:
        settings = load_settings()["planner"]
        mode = settings["default_mode"] if mode is None else mode
        scope = settings["recommend_scope"] if scope is None else scope
        top_n = settings["recommend_top_n"] if top_n is None else top_n
    available = rekey(have, mode)
    if isinstance(scope, str) or any(value not in ("core", "compare_only", "all") for value in scope):
        raise ValueError("추천 범위는 core, compare_only, all 목록이어야 합니다.")
    if type(top_n) is not int or top_n < 0:
        raise ValueError("추천 개수는 0 이상의 정수여야 합니다.")
    clauses = ["EXISTS (SELECT 1 FROM set_parts_flat f WHERE f.set_num = s.set_num)"]
    params = []
    if "all" not in scope:
        if not scope:
            return []
        clauses.append("c.scope IN (" + ",".join("?" for _ in scope) + ")")
        params.extend(scope)
    for column, low, high in (("year", year_min, year_max), ("num_parts", parts_min, parts_max)):
        for operator, value in ((">=", low), ("<=", high)):
            if value is not None:
                if type(value) is not int:
                    raise ValueError("연도와 부품 수 조건은 정수여야 합니다.")
                clauses.append(f"s.{column} {operator} ?")
                params.append(value)
    if top_n == 0:
        return []
    query = SET_SELECT + (" WHERE " + " AND ".join(clauses) if clauses else "")
    candidates = {row[0]: SetInfo(*row) for row in conn.execute(query, params)
                  if row[0] not in owned_set_nums}
    needs = {number: Counter() for number in candidates}
    # 필터 쿼리를 재사용하면 SQLite 바인딩 개수 제한과 N+1을 모두 피한다.
    for number, part, color, qty in conn.execute(
        "WITH candidates AS (" + query + ") "
        "SELECT f.set_num, f.part_num, f.color_id, f.quantity "
        "FROM candidates c JOIN set_parts_flat f ON f.set_num = c.set_num", params,
    ):
        if number in needs:
            needs[number][(part, color)] = qty
    results = []
    for number, parts in needs.items():
        need = rekey(parts, mode)
        missing = _missing(need, available, mode)
        total, shortage = sum(need.values()), sum(missing.values())
        if total == 0:
            continue
        info = candidates[number]
        results.append(Recommendation(info, shortage, len(missing),
                                      (total - shortage) / total if total else 0.0,
                                      total, total != info.num_parts))
    results.sort(key=lambda row: (row.total_missing, row.lots_missing, -row.coverage,
                                 row.set_info.year, row.set_info.set_num))
    return results[:top_n]
