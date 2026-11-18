"""PM 규칙표를 사용하는 결정적 세트·문서 매핑."""

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class EntityRule:
    kind: str
    pattern: re.Pattern[str]
    wiki_title: str
    ko_title: str | None
    index: int


def load_rules(path: Path) -> tuple[list[EntityRule], bool]:
    """규칙 순서와 목록 문서 허용 여부를 읽는다."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    include_list = data["include_list_pages"]
    if not isinstance(include_list, bool):
        raise ValueError("include_list_pages는 true 또는 false여야 합니다.")
    rules = [EntityRule(row["kind"], re.compile(row["pattern"], re.IGNORECASE),
                        row["wiki_title"], row.get("ko_title"), index)
             for index, row in enumerate(data["rules"])]
    return rules, include_list


def match_entities(name: str, rules: list[EntityRule], include_list: bool) -> list[EntityRule]:
    """매칭된 문서를 규칙 순서대로 중복 없이 반환한다."""
    result, seen = [], set()
    for rule in rules:
        if rule.kind == "list" and not include_list:
            continue
        if rule.wiki_title not in seen and rule.pattern.search(name):
            result.append(rule)
            seen.add(rule.wiki_title)
    return result


def build_set_entities(conn: sqlite3.Connection, rules: list[EntityRule],
                       include_list: bool, scopes: list[str]) -> dict[str, int]:
    """현재 범위로 파생 테이블을 교체한다. 제외된 세트의 매핑도 제거한다."""
    marks = ",".join("?" for _ in scopes)
    sets = conn.execute(f"""SELECT s.set_num, s.name FROM sets s
        JOIN set_scope sc ON sc.set_num=s.set_num WHERE sc.scope IN ({marks})
        ORDER BY s.set_num""", scopes).fetchall()
    rows = [(number, rule.wiki_title, rule.kind, rule.index)
            for number, name in sets for rule in match_entities(name, rules, include_list)]
    with conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS set_entities (
            set_num TEXT NOT NULL, wiki_title TEXT NOT NULL, kind TEXT NOT NULL,
            rule_index INTEGER NOT NULL, PRIMARY KEY (set_num, wiki_title))""")
        conn.execute("DELETE FROM set_entities")
        conn.executemany("INSERT INTO set_entities VALUES (?, ?, ?, ?)", rows)
    return {"sets": len(sets), "mapped": len({row[0] for row in rows}), "rows": len(rows)}
