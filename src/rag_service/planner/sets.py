"""세트 번호 정규화 및 메타데이터 조회."""

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class SetInfo:
    set_num: str
    name: str
    year: int
    num_parts: int
    theme: str
    scope: str | None


class SetNotFound(Exception):
    """존재하지 않는 세트와 결정적으로 정렬한 검색 후보."""

    def __init__(self, candidates: list[SetInfo]):
        super().__init__("세트를 찾을 수 없습니다. 번호 또는 후보 목록을 확인하세요.")
        self.candidates = candidates


SET_SELECT = """
    SELECT s.set_num, s.name, s.year, s.num_parts,
           COALESCE(t.root_theme_name, ''), c.scope
    FROM sets s
    LEFT JOIN set_root_theme t ON t.set_num = s.set_num
    LEFT JOIN set_scope c ON c.set_num = s.set_num
"""


def normalize_set_num(conn: sqlite3.Connection, raw: str) -> str:
    """공백을 제거하고 생략된 변형 번호를 DB에서 찾는다."""
    if not isinstance(raw, str):
        raise ValueError("세트 번호는 문자열이어야 합니다.")
    number = "".join(raw.split())
    row = conn.execute("SELECT set_num FROM sets WHERE set_num = ?", (number,)).fetchone()
    if row:
        return row[0]
    rows = conn.execute(
        "SELECT set_num FROM sets WHERE substr(set_num, 1, ?) = ?",
        (len(number) + 1, number + "-"),
    ) if number else ()
    variants = [r[0] for r in rows if r[0][len(number) + 1:].isdigit()]
    if variants:
        preferred = number + "-1"
        return preferred if preferred in variants else min(
            variants, key=lambda value: (int(value[len(number) + 1:]), value)
        )
    candidates = []
    if number:
        candidates = [SetInfo(*r) for r in conn.execute(
            SET_SELECT + """ WHERE substr(s.set_num, 1, ?) = ?
                OR instr(lower(s.name), lower(?)) > 0
                ORDER BY s.set_num LIMIT 5""",
            (len(number), number, raw.strip()),
        )]
    raise SetNotFound(candidates)


def get_set(conn: sqlite3.Connection, set_num: str) -> SetInfo:
    """정규화한 번호의 메타데이터를 반환한다."""
    number = normalize_set_num(conn, set_num)
    return SetInfo(*conn.execute(SET_SELECT + " WHERE s.set_num = ?", (number,)).fetchone())
