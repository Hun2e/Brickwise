"""출처와 검수 상태를 보존하는 한국어 별칭 사전."""

import sqlite3
from pathlib import Path

import yaml

from .entities import EntityRule


def build_ko_aliases(conn: sqlite3.Connection, rules: list[EntityRule],
                     manual_path: Path) -> dict[str, int]:
    """언어 링크를 우선하고 동일한 별칭·문서 쌍을 한 번만 저장한다."""
    rows = {}
    for rule in rules:
        if rule.ko_title:
            rows.setdefault((rule.ko_title, rule.wiki_title),
                            (rule.ko_title, rule.wiki_title, rule.kind, "wiki_langlink", 1))
    manual = yaml.safe_load(manual_path.read_text(encoding="utf-8"))
    for row in manual["aliases"]:
        if row["verified"] not in (0, 1):
            raise ValueError("별칭 verified는 0 또는 1이어야 합니다.")
        key = (row["alias"], row["canonical_en"])
        value = (*key, row["kind"], "manual", int(row["verified"]))
        previous = rows.get(key)
        if previous is None or (previous[3] == "manual" and value[4] > previous[4]):
            rows[key] = value
    with conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS ko_aliases (
            alias TEXT NOT NULL, canonical_en TEXT NOT NULL, kind TEXT NOT NULL,
            source TEXT NOT NULL, verified INTEGER NOT NULL CHECK (verified IN (0, 1)),
            PRIMARY KEY (alias, canonical_en))""")
        conn.execute("DELETE FROM ko_aliases")
        conn.executemany("INSERT INTO ko_aliases VALUES (?, ?, ?, ?, ?)", rows.values())
    return {source: sum(row[3] == source for row in rows.values())
            for source in ("wiki_langlink", "manual")}


def aliases_for(conn: sqlite3.Connection, wiki_titles: list[str]) -> tuple[list[str], list[str]]:
    """화면 표시 가능 별칭과 검색 전용 별칭을 각각 정렬해 반환한다."""
    marks = ",".join("?" for _ in wiki_titles)
    rows = conn.execute(f"""SELECT alias, MAX(verified) FROM ko_aliases
        WHERE canonical_en IN ({marks}) GROUP BY alias ORDER BY alias""", wiki_titles)
    verified, unverified = [], []
    for alias, checked in rows:
        (verified if checked else unverified).append(alias)
    return verified, unverified
