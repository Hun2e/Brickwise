"""T-009 질의의 손계산·경계 조건·실데이터 회귀."""

import sqlite3
from contextlib import closing
from dataclasses import replace

import pytest

from rag_service.config import load_settings, project_root, resolve_path
from rag_service.data_prep.rebrickable_loader import build_derived, load
from rag_service.data_prep.scope import select_scope
from rag_service.db.connection import connect
from rag_service.db.queries import FilterSpec, compare_versions, filter_sets, set_fact, set_ops
from rag_service.planner import NoInventory, SetNotFound


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "mini.sqlite"
    load(project_root() / "tests/fixtures/rebrickable_mini", path)
    with closing(connect(path)) as db:
        build_derived(db)
        select_scope(db, {**load_settings()["scope"], "parts_min": 2, "parts_max": 10,
                          "compare_keywords": ["Millennium Falcon", "X-wing", "TIE"],
                          "compare_parts_min": 2})
        yield db


@pytest.fixture(scope="module")
def real_conn():
    path = resolve_path(load_settings()["paths"]["sqlite"])
    if not path.is_file():
        pytest.skip("data/lego.sqlite가 없습니다.")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        yield db


def add_set(conn, number, name="Falcon", year=2020, official=7, scope="core", parts=()):
    conn.execute("INSERT INTO sets VALUES (?, ?, ?, 1, ?, '')", (number, name, year, official))
    if scope:
        conn.execute("INSERT INTO set_scope VALUES (?, ?)", (number, scope))
    conn.executemany("INSERT INTO set_parts_flat VALUES (?, ?, ?, ?)",
                     [(number, *row) for row in parts])


def test_fact_latest_inventory_and_short_names(conn):
    conn.executemany("INSERT INTO minifigs VALUES (?, ?, 0, '')",
                     [("t-1", "Han Solo, Coat"), ("t-2", "Han Solo - Jacket"), ("t-3", "Leia")])
    conn.executemany("INSERT INTO inventories VALUES (?, ?, 'A-1')",
                     [(9001, 10), (9002, 11), (9003, 11)])
    conn.executemany("INSERT INTO inventory_minifigs VALUES (?, ?, ?)",
                     [(9001, "t-3", 99), (9002, "t-3", 8), (9003, "t-1", 2), (9003, "t-2", 1)])
    fact = set_fact(conn, " A ")
    assert (fact.set_num, fact.num_parts, fact.agg_parts) == ("A-1", 7, 7)
    assert fact.minifigs == [("Han Solo", 3)]
    assert fact.entities == []
    assert set_fact(conn, "B").agg_parts == 5
    with pytest.raises(SetNotFound):
        set_fact(conn, "missing")
    rows, total = filter_sets(conn, FilterSpec(minifig="hAn", limit=1))
    assert total == 1 and rows[0].set_num == "A-1"
    assert filter_sets(conn, FilterSpec(minifig="Jacket")) == ([], 0)


@pytest.mark.parametrize("kwargs", [
    {"year_from": 2019}, {"year_to": 2018}, {"parts_min": 7}, {"parts_max": 6},
    {"subtheme": "Star Wars"}, {"has_minifig": True}, {"has_minifig": False},
    {"name_keywords": ("Falcon",)}, {"name_keywords": ("Falcon", "Millennium")},
    {"scopes": ("compare_only",)}, {"scopes": ()},
])
def test_filter_conditions(conn, kwargs):
    all_facts = [set_fact(conn, row[0]) for row in conn.execute("SELECT set_num FROM set_scope")]
    spec = FilterSpec(**kwargs)
    expected = [f for f in all_facts if f.scope in spec.scopes
                and (spec.year_from is None or f.year >= spec.year_from)
                and (spec.year_to is None or f.year <= spec.year_to)
                and (spec.parts_min is None or f.num_parts >= spec.parts_min)
                and (spec.parts_max is None or f.num_parts <= spec.parts_max)
                and (spec.subtheme is None or f.subtheme == spec.subtheme)
                and (spec.has_minifig is None or bool(f.minifigs) == spec.has_minifig)
                and all(word.lower() in f.name.lower() for word in spec.name_keywords)]
    expected.sort(key=lambda f: (f.year, f.set_num))
    assert filter_sets(conn, spec) == (expected, len(expected))


@pytest.mark.parametrize("sort", ["year", "num_parts", "name", "set_num"])
@pytest.mark.parametrize("order", ["asc", "desc"])
def test_filter_sort_total_limit_and_determinism(conn, sort, order):
    add_set(conn, "SORT-2", name="same", year=2020, official=8)
    add_set(conn, "SORT-1", name="same", year=2020, official=8)
    add_set(conn, "SORT-3", name="z", year=2021, official=9)
    spec = FilterSpec(name_keywords=("",), sort=sort, order=order, limit=100)
    rows, total = filter_sets(conn, spec)
    expected = sorted(sorted(rows, key=lambda f: f.set_num),
                      key=lambda f: getattr(f, sort), reverse=order == "desc")
    assert rows == expected and total == len(rows)
    assert filter_sets(conn, spec) == (rows, total)
    assert filter_sets(conn, replace(spec, limit=1)) == (rows[:1], total)
    assert filter_sets(conn, replace(spec, limit=0)) == ([], total)


@pytest.mark.parametrize("kwargs", [
    {"sort": "year; DROP TABLE sets"}, {"order": "desc;--"}, {"limit": -1}, {"limit": True},
    {"year_from": "2019"}, {"parts_max": 1.5}, {"scopes": ("all",)},
    {"scopes": "core"}, {"has_minifig": 1},
])
def test_filter_invalid_options(conn, kwargs):
    with pytest.raises(ValueError):
        filter_sets(conn, FilterSpec(**kwargs))


@pytest.mark.parametrize("value", ["%", "_", "' OR 1=1 --"])
def test_filter_values_are_literal(conn, value):
    assert filter_sets(conn, FilterSpec(name_keywords=(value,))) == ([], 0)
    assert filter_sets(conn, FilterSpec(subtheme=value)) == ([], 0)
    assert filter_sets(conn, FilterSpec(minifig=value)) == ([], 0)


@pytest.mark.parametrize("mode, expected", [
    ("exact", (1, 2, 2, 7, 2, 10, 2)),
    ("ignore_color", (2, 7, 0, 0, 1, 5, 2)),
])
def test_setops_hand_calculated(conn, mode, expected):
    add_set(conn, "LEFT-1", parts=[("p1", 1, 4), ("p1", 2, 2), ("p2", 1, 5)])
    add_set(conn, "RIGHT-1", parts=[("p1", 1, 2), ("p2", 2, 5), ("p3", 1, 5)])
    r = set_ops(conn, "LEFT", "RIGHT", mode)
    assert (r.common_lots, r.common_qty, r.only_a_lots, r.only_a_qty,
            r.only_b_lots, r.only_b_qty, r.common_lots_ignore_color) == expected
    assert [(p, c, q) for p, c, _, q in r.top_common] == (
        [("p1", 1, 2)] if mode == "exact" else [("p2", None, 5), ("p1", None, 2)])
    assert r == set_ops(conn, "LEFT", "RIGHT", mode)
    reverse = set_ops(conn, "RIGHT", "LEFT", mode)
    assert (reverse.common_lots, reverse.common_qty) == (r.common_lots, r.common_qty)
    assert reverse.only_a_qty == r.only_b_qty


def test_setops_identity_disjoint_errors_and_config(conn, monkeypatch):
    r = set_ops(conn, "A", "A")
    assert (r.common_lots, r.common_qty, r.only_a_lots, r.only_b_qty) == (3, 7, 0, 0)
    add_set(conn, "DISJOINT-1", parts=[("new", 1, 3)])
    r = set_ops(conn, "A", "DISJOINT")
    assert (r.common_lots, r.common_qty, r.top_common) == (0, 0, [])
    for a, b in (("D", "A"), ("A", "D")):
        with pytest.raises(NoInventory):
            set_ops(conn, a, b)
    with pytest.raises(SetNotFound):
        set_ops(conn, "A", "missing")
    with pytest.raises(ValueError):
        set_ops(conn, "A", "B", "bad")
    settings = load_settings()
    settings["compare"]["set_ops_top_n"] = 1
    monkeypatch.setattr("rag_service.db.queries.load_settings", lambda: settings)
    assert len(set_ops(conn, "A", "A").top_common) == 1


def test_versions_boundaries_grouping_scope_and_vehicle(conn):
    conn.execute("CREATE TABLE set_entities(set_num TEXT, wiki_title TEXT, kind TEXT, rule_index INTEGER)")
    examples = [
        ("V-1", "Falcon Blue Box", 2004, 997), ("V-2", "Falcon Black Box", 2004, 997),
        ("V-3", "Falcon", 2005, 997), ("V-4", "Falcon", 2004, 998),
        ("M-1", "Falcon Minifig", 2020, 7), ("ANN-1", "Falcon 20th Anniversary", 2019, 7),
        ("MINI-1", "Falcon MINI", 2020, 7), ("MIDI-1", "Falcon Midi-Scale", 2020, 7),
        ("MICRO-1", "Falcon Microfighter", 2020, 7), ("COCK-1", "Falcon Cockpit", 2020, 7),
        ("DIO-1", "Falcon Holiday Diorama", 2020, 7),
    ]
    for number, name, year, official in examples:
        add_set(conn, number, name, year, official, scope="compare_only")
        conn.execute("INSERT INTO set_entities VALUES (?, 'Falcon', 'vehicle', 0)", (number,))
    add_set(conn, "OUT-1", scope=None)
    conn.execute("INSERT INTO set_entities VALUES ('OUT-1', 'Falcon', 'vehicle', 0)")
    add_set(conn, "CHAR-1")
    conn.execute("INSERT INTO set_entities VALUES ('CHAR-1', 'Falcon', 'character', 0)")
    r = compare_versions(conn, "V-1", load_settings())
    assert r.entity == "Falcon" and not r.warnings
    assert [row["set_nums"] for row in r.rows] == [["V-1", "V-2"], ["V-4"], ["V-3"], ["ANN-1"], ["M-1"]]
    assert {number for number, _ in r.excluded} == {"MINI-1", "MIDI-1", "MICRO-1", "COCK-1", "DIO-1"}
    assert "Blue Box" in r.rows[0]["name"] and "Black Box" in r.rows[0]["name"]
    settings = load_settings()
    settings["compare"]["variant_markers"] = []
    assert not compare_versions(conn, "V-1", settings).excluded


def test_versions_fallback_is_explicit(conn):
    add_set(conn, "FALL-1", "Millennium Falcon", scope="compare_only")
    r = compare_versions(conn, "FALL", load_settings())
    assert r.entity == "Millennium Falcon" and r.warnings
    assert any("FALL-1" in row["set_nums"] for row in r.rows)
    with pytest.raises(SetNotFound):
        compare_versions(conn, "absent", load_settings())


def test_real_counts_and_ucs(real_conn):
    assert set_fact(real_conn, "75192-1").num_parts == 7541
    fact = set_fact(real_conn, "75257-1")
    assert (fact.num_parts, fact.agg_parts) == (1328, 1351)
    r = set_ops(real_conn, "75257-1", "75375-1")
    assert (r.common_lots, r.common_qty, r.common_lots_ignore_color) == (79, 196, 100)
    rows, total = filter_sets(real_conn, FilterSpec(subtheme="Ultimate Collector Series"))
    assert total == len(rows) == 16
    assert all(row.subtheme == "Ultimate Collector Series" for row in rows)


def test_default_scopes_follow_settings_and_explicit_scopes(conn, monkeypatch):
    assert FilterSpec().scopes == ("core", "compare_only")
    settings = load_settings()
    settings["search"]["default_scopes"] = ["compare_only"]
    monkeypatch.setattr("rag_service.db.queries.load_settings", lambda: settings)
    default = FilterSpec()
    assert default.scopes == ("compare_only",)
    rows, total = filter_sets(conn, default)
    assert total > 0 and all(f.scope == "compare_only" for f in rows)
    explicit = FilterSpec(scopes=("core",))
    assert all(f.scope == "core" for f in filter_sets(conn, explicit)[0])
    assert filter_sets(conn, FilterSpec(scopes=())) == ([], 0)
    settings["search"]["default_scopes"] = ["core"]
    assert FilterSpec().scopes == ("core",)
    assert default.scopes == ("compare_only",)


def test_real_versions(real_conn):
    r = compare_versions(real_conn, "75257-1", load_settings())
    assert not r.warnings
    assert [row["year"] for row in r.rows] == sorted(row["year"] for row in r.rows)
    assert sum(row["set_nums"] == ["4504-1", "4504-2"] for row in r.rows) == 1
    excluded = {number for number, _ in r.excluded}
    assert {"75295-1", "75512-1", "40658-1", "7778-1"} <= excluded
    included = {number for row in r.rows for number in row["set_nums"]}
    anniversaries = {row[0] for row in real_conn.execute("""SELECT s.set_num FROM sets s
        JOIN set_entities e ON e.set_num=s.set_num JOIN set_scope sc ON sc.set_num=s.set_num
        WHERE e.kind='vehicle' AND e.wiki_title='Millennium Falcon'
        AND lower(s.name) LIKE '%20th anniversary%'""")}
    assert anniversaries <= included


def test_exclude_keywords_boundaries_case_total_and_limit(conn):
    add_set(conn, "EX-1", "Battle Pack", year=2000)
    add_set(conn, "EX-2", "BATTLE PACK - Troopers", year=2001)
    add_set(conn, "EX-3", "Battle Backpack", year=2002)
    add_set(conn, "EX-4", "Battle Packer", year=2003)
    add_set(conn, "EX-5", "TIE Fighter", year=2004)
    add_set(conn, "EX-6", "Frontier", year=2005)
    spec = FilterSpec(exclude_keywords=("battle pack", "tie"), limit=1)
    rows, total = filter_sets(conn, replace(spec, limit=100))
    ids = {row.set_num for row in rows}
    assert not {"EX-1", "EX-2", "EX-5"} & ids
    assert {"EX-3", "EX-4", "EX-6"} <= ids
    assert filter_sets(conn, spec) == (rows[:1], total)
    assert total == filter_sets(conn, FilterSpec(limit=100))[1] - 3
    assert filter_sets(conn, FilterSpec(exclude_keywords=())) == filter_sets(conn, FilterSpec())


@pytest.mark.parametrize("word", ["%", "_", "' OR 1=1 --", "[", ".*"])
def test_exclude_keywords_are_literal(conn, word):
    assert filter_sets(conn, FilterSpec(exclude_keywords=(word,))) == filter_sets(conn, FilterSpec())
