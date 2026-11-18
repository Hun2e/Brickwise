"""화면이 DB나 도메인 매핑을 알 필요 없는 세트 선택 목록."""

import sqlite3
from contextlib import closing
from dataclasses import dataclass

from rag_service.config import load_settings, resolve_path


@dataclass(frozen=True)
class SetOption:
    set_num: str
    name: str
    year: int
    num_parts: int
    scope: str
    aliases: list[str]
    img_url: str

    def label(self) -> str:
        alias = f" · {self.aliases[0]}" if self.aliases else ""
        suffix = " (비교용)" if self.scope == "compare_only" else ""
        return f"{self.set_num} · {self.name} ({self.year}){alias}{suffix}"


def list_sets(conn: sqlite3.Connection) -> list[SetOption]:
    aliases: dict[str, list[str]] = {}
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if {"ko_aliases", "set_entities"} <= tables:
        for number, alias, _ in conn.execute(
            "SELECT e.set_num, a.alias, MAX(a.verified) FROM set_entities e "
            "JOIN ko_aliases a ON a.canonical_en=e.wiki_title "
            "GROUP BY e.set_num, a.alias ORDER BY e.set_num, MAX(a.verified) DESC, a.alias"
        ):
            aliases.setdefault(number, []).append(alias)
    rows = conn.execute(
        "SELECT s.set_num, s.name, s.year, s.num_parts, sc.scope, s.img_url "
        "FROM sets s JOIN set_scope sc ON s.set_num=sc.set_num "
        "ORDER BY CASE sc.scope WHEN 'core' THEN 0 ELSE 1 END, s.year DESC, s.set_num"
    )
    return [SetOption(number, name, year, parts, scope, aliases.get(number, []), image or "")
            for number, name, year, parts, scope, image in rows]


def catalog_options() -> list[SetOption]:
    path = resolve_path(load_settings()["paths"]["sqlite"])
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
        return list_sets(conn)
