"""설정에 따른 core/compare_only 세트 선정."""

import sqlite3


def select_scope(conn: sqlite3.Connection, scope_cfg: dict) -> dict[str, int]:
    """부품이 있는 세트만 고르고 core가 비교 전용보다 우선하도록 재생성한다."""
    with conn:
        conn.execute("DELETE FROM set_scope")
        conn.execute("""
            INSERT INTO set_scope
            SELECT s.set_num, 'core' FROM sets s JOIN set_root_theme r USING (set_num)
            WHERE r.root_theme_name = ? AND s.year BETWEEN ? AND ?
              AND s.num_parts BETWEEN ? AND ?
              AND EXISTS (SELECT 1 FROM set_parts_flat p WHERE p.set_num = s.set_num)
        """, (
            scope_cfg["root_theme"], scope_cfg["year_min"], scope_cfg["year_max"],
            scope_cfg["parts_min"], scope_cfg["parts_max"],
        ))
        keywords = scope_cfg["compare_keywords"]
        if keywords:
            # LIKE의 %/_ 와일드카드 해석 없이 문자열 포함 조건을 적용한다.
            condition = " OR ".join("instr(lower(s.name), lower(?)) > 0" for _ in keywords)
            conn.execute(f"""
                INSERT INTO set_scope
                SELECT s.set_num, 'compare_only' FROM sets s JOIN set_root_theme r USING (set_num)
                WHERE r.root_theme_name = ? AND s.num_parts >= ? AND ({condition})
                  AND NOT EXISTS (SELECT 1 FROM set_scope c WHERE c.set_num = s.set_num)
                  AND EXISTS (SELECT 1 FROM set_parts_flat p WHERE p.set_num = s.set_num)
            """, (scope_cfg["root_theme"], scope_cfg["compare_parts_min"], *keywords))
    counts = dict(conn.execute("SELECT scope, COUNT(*) FROM set_scope GROUP BY scope"))
    return {scope: counts.get(scope, 0) for scope in ("core", "compare_only")}
