"""읽기 전용 DB 질의와 결정적 집합·판본 비교."""

import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field

from rag_service.config import load_settings
from rag_service.data_prep.item_text import short_minifig_name
from rag_service.planner.inventory import rekey, set_parts
from rag_service.planner.matcher import NoInventory
from rag_service.planner.sets import get_set, normalize_set_num


@dataclass(frozen=True)
class SetFact:
    set_num: str
    name: str
    year: int
    num_parts: int
    agg_parts: int
    subtheme: str
    scope: str | None
    minifigs: list[tuple[str, int]]
    entities: list[str]


def _entities(conn: sqlite3.Connection, number: str, kind: str | None = None) -> list[str]:
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='set_entities'").fetchone():
        return []
    return [row[0] for row in conn.execute(
        "SELECT wiki_title FROM set_entities WHERE set_num=?"
        + (" AND kind=?" if kind else "") + " ORDER BY rule_index, wiki_title",
        (number, kind) if kind else (number,),
    )]


def set_fact(conn: sqlite3.Connection, set_num: str) -> SetFact:
    """최신 인벤토리의 미니피그를 짧은 이름별로 합산한다."""
    info = get_set(conn, set_num)
    subtheme = conn.execute("""SELECT COALESCE(rt.subtheme_name, t.name, '')
        FROM sets s LEFT JOIN set_root_theme rt ON rt.set_num=s.set_num
        LEFT JOIN themes t ON t.id=s.theme_id WHERE s.set_num=?""", (info.set_num,)).fetchone()[0]
    figs: Counter[str] = Counter()
    prefixes = load_settings()["index"]["generic_minifig_prefixes"]
    for name, qty in conn.execute("""SELECT COALESCE(m.name, im.fig_num), im.quantity
        FROM inventory_minifigs im LEFT JOIN minifigs m ON m.fig_num=im.fig_num
        WHERE im.inventory_id=(SELECT id FROM inventories WHERE set_num=?
            ORDER BY version DESC, id DESC LIMIT 1)""", (info.set_num,)):
        figs[short_minifig_name(name, prefixes)] += qty
    total = conn.execute("SELECT COALESCE(SUM(quantity), 0) FROM set_parts_flat WHERE set_num=?",
                         (info.set_num,)).fetchone()[0]
    return SetFact(info.set_num, info.name, info.year, info.num_parts, total, subtheme,
                   info.scope, sorted(figs.items()), _entities(conn, info.set_num))


@dataclass(frozen=True)
class FilterSpec:
    year_from: int | None = None
    year_to: int | None = None
    parts_min: int | None = None
    parts_max: int | None = None
    subtheme: str | None = None
    minifig: str | None = None
    has_minifig: bool | None = None
    name_keywords: tuple[str, ...] = ()
    scopes: tuple[str, ...] = field(default_factory=lambda: tuple(load_settings()["search"]["default_scopes"]))
    sort: str = "year"
    order: str = "asc"
    limit: int = 50
    exclude_keywords: tuple[str, ...] = ()
    set_nums: tuple[str, ...] = ()


def filter_sets(conn: sqlite3.Connection, spec: FilterSpec) -> tuple[list[SetFact], int]:
    """SQL 조건을 바인딩하고 짧은 미니피그 이름 조건을 적용한 뒤 제한한다."""
    sorts = {"year": "s.year", "num_parts": "s.num_parts", "name": "s.name", "set_num": "s.set_num"}
    if spec.sort not in sorts or spec.order not in ("asc", "desc"):
        raise ValueError("정렬 열과 방향을 확인하세요.")
    if type(spec.limit) is not int or spec.limit < 0:
        raise ValueError("결과 개수는 0 이상의 정수여야 합니다.")
    if isinstance(spec.scopes, str) or any(s not in ("core", "compare_only") for s in spec.scopes):
        raise ValueError("범위는 core, compare_only 목록이어야 합니다.")
    if spec.has_minifig is not None and type(spec.has_minifig) is not bool:
        raise ValueError("미니피그 포함 조건은 참 또는 거짓이어야 합니다.")
    clauses = ["sc.scope IN (" + ",".join("?" for _ in spec.scopes) + ")"]
    params = list(spec.scopes)
    if isinstance(spec.set_nums, str) or any(not isinstance(n, str) for n in spec.set_nums):
        raise ValueError("허용 세트 번호는 문자열 목록이어야 합니다.")
    if spec.set_nums:
        clauses.append("s.set_num IN (" + ",".join("?" for _ in spec.set_nums) + ")")
        params.extend(spec.set_nums)
    for column, low, high in (("year", spec.year_from, spec.year_to),
                              ("num_parts", spec.parts_min, spec.parts_max)):
        for op, value in ((">=", low), ("<=", high)):
            if value is not None:
                if type(value) is not int:
                    raise ValueError("연도와 부품 수는 정수여야 합니다.")
                clauses.append(f"s.{column} {op} ?")
                params.append(value)
    if spec.subtheme is not None:
        clauses.append("COALESCE(rt.subtheme_name, t.name, '') = ? COLLATE NOCASE")
        params.append(spec.subtheme)
    for word in spec.name_keywords:
        clauses.append("instr(lower(s.name), lower(?)) > 0")
        params.append(word)
    query = """SELECT s.set_num FROM sets s JOIN set_scope sc ON sc.set_num=s.set_num
        LEFT JOIN set_root_theme rt ON rt.set_num=s.set_num
        LEFT JOIN themes t ON t.id=s.theme_id WHERE """ + " AND ".join(clauses)
    query += f" ORDER BY {sorts[spec.sort]} {spec.order}, s.set_num ASC"
    results = []
    excluded = [re.compile(r"(?<!\w)" + re.escape(word.strip()) + r"(?!\w)", re.I)
                for word in spec.exclude_keywords if word.strip()]
    for (number,) in conn.execute(query, params):
        fact = set_fact(conn, number)
        if any(pattern.search(fact.name) for pattern in excluded):
            continue
        has_fig = sum(qty for _, qty in fact.minifigs) > 0
        if spec.has_minifig is not None and has_fig != spec.has_minifig:
            continue
        if spec.minifig is not None and not any(
            spec.minifig.casefold() in name.casefold() for name, qty in fact.minifigs if qty > 0
        ):
            continue
        results.append(fact)
    return results[:spec.limit], len(results)


@dataclass(frozen=True)
class SetOps:
    a: str
    b: str
    mode: str
    common_lots: int
    common_qty: int
    only_a_lots: int
    only_a_qty: int
    only_b_lots: int
    only_b_qty: int
    common_lots_ignore_color: int
    top_common: list[tuple[str, int | None, str, int]]
    facts: tuple[SetFact, ...] = ()


def set_ops(conn: sqlite3.Connection, a: str, b: str, mode: str = "exact") -> SetOps:
    """A만/B만은 상대에 없는 키의 전체 수량이며 공통 키 잔량은 제외한다."""
    rekey(Counter(), mode)
    a, b = normalize_set_num(conn, a), normalize_set_num(conn, b)
    raw_a, raw_b = set_parts(conn, a), set_parts(conn, b)
    for number, parts in ((a, raw_a), (b, raw_b)):
        if not +parts:
            raise NoInventory(number)
    left, right = rekey(raw_a, mode), rekey(raw_b, mode)
    common = left & right
    only_a, only_b = left.keys() - right.keys(), right.keys() - left.keys()
    names = dict(conn.execute("""SELECT DISTINCT f.part_num, COALESCE(p.name, f.part_num)
        FROM set_parts_flat f LEFT JOIN parts p ON p.part_num=f.part_num WHERE f.set_num=?""", (a,)))
    rows = [(key[0], key[1], names[key[0]], qty) if mode == "exact"
            else (key, None, names[key], qty) for key, qty in common.items()]
    rows.sort(key=lambda row: (-row[3], row[0], row[1] if row[1] is not None else -1))
    return SetOps(a, b, mode, len(common), sum(common.values()),
                  len(only_a), sum(left[k] for k in only_a), len(only_b), sum(right[k] for k in only_b),
                  len(rekey(raw_a, "ignore_color") & rekey(raw_b, "ignore_color")),
                  rows[:load_settings()["compare"]["set_ops_top_n"]],
                  (set_fact(conn, a), set_fact(conn, b)))


@dataclass(frozen=True)
class VersionCompare:
    entity: str
    rows: list[dict]
    excluded: list[tuple[str, str]]
    warnings: list[str]


def compare_versions(conn: sqlite3.Connection, set_num: str, settings: dict) -> VersionCompare:
    """vehicle 연결 세트를 비교하고 동일 번호 계열·연도·부품 수를 묶는다."""
    selected = set_fact(conn, set_num)
    vehicles = _entities(conn, selected.set_num, "vehicle")
    warnings = []
    if vehicles:
        marks = ",".join("?" for _ in vehicles)
        numbers = [r[0] for r in conn.execute(f"""SELECT DISTINCT e.set_num FROM set_entities e
            JOIN set_scope sc ON sc.set_num=e.set_num
            WHERE e.kind='vehicle' AND e.wiki_title IN ({marks})
            AND sc.scope IN ('core', 'compare_only') ORDER BY e.set_num""", vehicles)]
        entity = ", ".join(vehicles)
    else:
        keywords = [word for word in settings["scope"]["compare_keywords"]
                    if word.casefold() in selected.name.casefold()]
        keywords = keywords or [selected.name]
        clauses = " OR ".join("instr(lower(s.name), lower(?)) > 0" for _ in keywords)
        numbers = [r[0] for r in conn.execute("""SELECT s.set_num FROM sets s
            JOIN set_scope sc ON sc.set_num=s.set_num
            WHERE sc.scope IN ('core', 'compare_only') AND (""" + clauses + ") ORDER BY s.set_num", keywords)]
        entity = ", ".join(keywords)
        warnings.append("vehicle 엔티티가 없어 이름 키워드로 판본 후보를 조회했습니다.")
    markers = [re.compile(r"\b" + re.escape(word) + r"\b", re.I)
               for word in settings["compare"]["variant_markers"] if word]
    groups: dict[tuple, list[SetFact]] = {}
    excluded = []
    for number in numbers:
        fact = set_fact(conn, number)
        if any(marker.search(fact.name) for marker in markers):
            excluded.append((fact.set_num, fact.name))
        else:
            groups.setdefault((fact.set_num.split("-")[0], fact.year, fact.num_parts), []).append(fact)
    rows = []
    for facts in groups.values():
        first = facts[0]
        # 병합한 판본에서 다른 값은 조용히 버리지 않고 표에 병기한다.
        fig_counts = sorted({sum(qty for _, qty in f.minifigs) for f in facts})
        rows.append({"set_num": "/".join(f.set_num for f in facts),
                     "set_nums": [f.set_num for f in facts],
                     "name": " / ".join(dict.fromkeys(f.name for f in facts)),
                     "year": first.year, "num_parts": first.num_parts,
                     "minifig_count": fig_counts[0] if len(fig_counts) == 1 else "/".join(map(str, fig_counts)),
                     "subtheme": " / ".join(dict.fromkeys(f.subtheme for f in facts)),
                     "scope": " / ".join(dict.fromkeys(f.scope or "범위 밖" for f in facts)),
                     "agg_parts": {f.set_num: f.agg_parts for f in facts}})
    rows.sort(key=lambda row: (row["year"], row["set_nums"][0]))
    return VersionCompare(entity, rows, excluded, warnings)
