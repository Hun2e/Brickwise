"""T-001 소형 CSV와 손계산으로 planner 계약을 검증한다."""

from collections import Counter
from contextlib import closing
from uuid import UUID

import pytest

from rag_service.config import load_settings, project_root
from rag_service.data_prep.rebrickable_loader import build_derived, load
from rag_service.data_prep.scope import select_scope
from rag_service.db.connection import connect
from rag_service.planner import (
    CollectionStore, MissingRow, NoInventory, SetNotFound, analyze, collection_have,
    get_set, normalize_set_num, recommend, rekey, set_parts,
)


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


def add_set(conn, number, year=2020, parts=(), scope="core", official=None):
    total = sum(qty for _, _, qty in parts)
    conn.execute("INSERT INTO sets VALUES (?, ?, ?, 1, ?, '')",
                 (number, "Test " + number, year, total if official is None else official))
    if scope:
        conn.execute("INSERT INTO set_scope VALUES (?, ?)", (number, scope))
    conn.executemany("INSERT INTO set_parts_flat VALUES (?, ?, ?, ?)",
                     [(number, *row) for row in parts])


def test_normalization_and_candidates(conn):
    for raw in ("A", " A ", " A - 1 ", "A-1"):
        assert normalize_set_num(conn, raw) == "A-1"
    for number in ("VAR-10", "VAR-2", "VAR-3"):
        add_set(conn, number)
    assert normalize_set_num(conn, "VAR") == "VAR-2"
    add_set(conn, "VAR-1")
    assert normalize_set_num(conn, "VAR") == "VAR-1"
    assert get_set(conn, "A").theme == "Star Wars"
    assert get_set(conn, "G").scope is None
    for number in ("VAR-", "Test VAR", "not-a-set", "", "%", "' OR 1=1 --", "VAR-99"):
        with pytest.raises(SetNotFound) as exc:
            normalize_set_num(conn, number)
        candidates = exc.value.candidates
        assert len(candidates) <= 5
        assert candidates == sorted(candidates, key=lambda info: info.set_num)
        if number in ("VAR-", "Test VAR"):
            assert len(candidates) == 4
        else:
            assert candidates == []
    for i in range(11, 18):
        add_set(conn, f"VAR-{i}")
    with pytest.raises(SetNotFound) as exc:
        normalize_set_num(conn, "Test VAR")
    assert len(exc.value.candidates) == 5


def test_collection_aggregation_and_adjustments(conn):
    assert collection_have(conn, [("A", 2)]) == Counter({("p1", 1): 4, ("p2", 1): 6, ("p1", 2): 4})
    adjustments = [("p1", 1, -9), ("p1", 1, 8), ("p2", 1, -3), ("extra", 2, 4), ("lost", 2, -1)]
    result = collection_have(conn, [("A-1", 1)], adjustments)
    assert result == Counter({("p1", 1): 1, ("p1", 2): 2, ("extra", 2): 4})
    assert result == collection_have(conn, [("A-1", 1)], list(reversed(adjustments)))
    assert collection_have(conn, []) == Counter()
    assert set_parts(conn, "D") == Counter()
    with pytest.raises(SetNotFound):
        set_parts(conn, "missing")


@pytest.mark.parametrize("qty", [0, -1, True, 1.5, "2"])
def test_bad_quantity(conn, qty):
    with pytest.raises(ValueError):
        collection_have(conn, [("A", qty)])


def test_hand_calculated_analysis(conn):
    have = Counter({("p1", 1): 1, ("p1", 2): 10, ("p2", 2): 3})
    before = have.copy()
    result = analyze(conn, have, "A", "exact")
    names = dict(conn.execute("SELECT part_num, name FROM parts"))
    colors = dict(conn.execute("SELECT id, name FROM colors"))
    assert (result.need_total, result.have_matched, result.total_missing, result.lots_missing) == (7, 3, 4, 2)
    assert result.coverage == 3 / 7
    assert result.official_num_parts == 7 and not result.count_mismatch
    assert result.rows == [MissingRow("p2", 1, names["p2"], colors[1], 3, 0, 3),
                           MissingRow("p1", 1, names["p1"], colors[1], 2, 1, 1)]
    ignored = analyze(conn, have, "A", "ignore_color")
    assert (ignored.total_missing, ignored.lots_missing, ignored.coverage, ignored.rows) == (0, 0, 1.0, [])
    ignored = analyze(conn, Counter({("p1", 2): 3}), "A", "ignore_color")
    assert ignored.rows == [MissingRow("p2", None, names["p2"], None, 3, 0, 3),
                            MissingRow("p1", None, names["p1"], None, 4, 3, 1)]
    assert have == before
    assert rekey(have, "exact") == before
    assert rekey(have, "ignore_color") == Counter(p1=11, p2=3)


def test_unknown_and_row_sort(conn):
    add_set(conn, "UNKNOWN-1", parts=[("p1", -1, 2), ("p1", 1, 2), ("p2", 2, 2)])
    have = Counter({("p1", -1): 20})
    result = analyze(conn, have, "UNKNOWN-1", "exact")
    assert (result.total_missing, result.have_matched, result.coverage) == (6, 0, 0.0)
    assert [(r.part_num, r.color_id, r.have) for r in result.rows] == [("p1", -1, 0), ("p1", 1, 0), ("p2", 2, 0)]
    assert analyze(conn, have, "UNKNOWN-1", "ignore_color").total_missing == 2
    mismatch = analyze(conn, Counter(), "B-1")
    assert (mismatch.official_num_parts, mismatch.need_total, mismatch.count_mismatch) == (6, 5, True)


@pytest.mark.parametrize("mode", ["exact", "ignore_color"])
def test_missing_inventory_is_not_buildable(conn, mode):
    have = set_parts(conn, "A")
    assert set_parts(conn, "D") == Counter()
    add_set(conn, "ZERO-1", parts=[("p1", 1, 0)])
    for target in ("D", "ZERO"):
        with pytest.raises(NoInventory) as exc:
            analyze(conn, have, target, mode)
        assert exc.value.set_num == target + "-1"
    result = recommend(conn, have, set(), mode=mode, scope=["all"], top_n=100)
    numbers = {row.set_info.set_num for row in result}
    assert "D-1" not in numbers
    assert "ZERO-1" not in numbers
    assert "A-1" in numbers and "G-1" in numbers
    assert all(row.need_total > 0 for row in result)
    assert recommend(conn, have, numbers, mode=mode, scope=["all"]) == []


@pytest.mark.parametrize("mode", ["mold_variant", "invalid", ""])
def test_unsupported_modes(conn, mode):
    for call in (lambda: rekey(Counter(), mode), lambda: analyze(conn, Counter(), "A", mode),
                 lambda: recommend(conn, Counter(), set(), mode=mode, top_n=0)):
        with pytest.raises(ValueError):
            call()


def test_recommend_filters_and_batch_queries(conn):
    have = set_parts(conn, "A")
    statements = []
    conn.set_trace_callback(statements.append)
    result = recommend(conn, have, {"A-1"})
    conn.set_trace_callback(None)
    assert [r.set_info.set_num for r in result] == ["B-1", "C-1"]
    assert len(statements) == 2
    assert sum("JOIN set_parts_flat" in sql for sql in statements) == 1
    assert result == recommend(conn, have, {"A-1"})
    for row in result:
        analysis = analyze(conn, have, row.set_info.set_num)
        assert (row.total_missing, row.lots_missing, row.coverage, row.need_total, row.count_mismatch) == (
            analysis.total_missing, analysis.lots_missing, analysis.coverage, analysis.need_total, analysis.count_mismatch)
    target = get_set(conn, "A")
    filtered = recommend(conn, have, set(), year_min=target.year, year_max=target.year,
                         parts_min=target.num_parts, parts_max=target.num_parts)
    assert "A-1" in [r.set_info.set_num for r in filtered]
    assert all(r.set_info.year == target.year and r.set_info.num_parts == target.num_parts for r in filtered)
    assert {r.set_info.scope for r in recommend(conn, have, set(), scope=["compare_only"])} == {"compare_only"}
    assert "G-1" in {r.set_info.set_num for r in recommend(conn, have, set(), scope=["all"], top_n=100)}
    assert recommend(conn, have, set(), scope=[]) == []
    assert recommend(conn, have, set(), year_min=3000) == []
    assert recommend(conn, have, set(), top_n=0) == []
    assert recommend(conn, have, set(), top_n=1) == recommend(conn, have, set())[:1]
    with pytest.raises(ValueError):
        recommend(conn, have, set(), scope=["invalid"])
    with pytest.raises(ValueError):
        recommend(conn, have, set(), top_n=-1)


def test_recommend_every_sort_priority(conn):
    # 부족 수 -> 종류 수 -> coverage -> 연도 -> 번호를 각각 충돌시킨다.
    examples = [
        ("R-1", 2020, [("p2", 1, 1)]),
        ("R-2", 2020, [("p1", 1, 8), ("p2", 1, 1), ("p2", 2, 1)]),
        ("R-3", 2020, [("p2", 1, 2)]),
        ("R-4", 2021, [("p1", 1, 8), ("p2", 1, 2)]),
        ("R-6", 2020, [("p1", 1, 8), ("p2", 1, 2)]),
        ("R-5", 2020, [("p1", 1, 8), ("p2", 1, 2)]),
    ]
    for number, year, parts in examples:
        add_set(conn, number, year, parts, scope="compare_only")
    result = recommend(conn, Counter({("p1", 1): 8}), {"E-1", "I-1"}, scope=["compare_only"])
    assert [r.set_info.set_num for r in result] == ["R-1", "R-5", "R-6", "R-4", "R-3", "R-2"]


def test_config_defaults(conn, monkeypatch):
    import importlib
    settings = {"planner": {"default_mode": "ignore_color", "recommend_scope": ["compare_only"], "recommend_top_n": 1}}
    for module in ("matcher", "recommend"):
        monkeypatch.setattr(importlib.import_module("rag_service.planner." + module), "load_settings", lambda: settings)
    assert analyze(conn, Counter(), "A").mode == "ignore_color"
    result = recommend(conn, Counter(), set())
    assert len(result) == 1 and result[0].set_info.scope == "compare_only"


def test_collection_store_lifecycle(tmp_path):
    path = tmp_path / "users/user.sqlite"
    store = CollectionStore(path)
    first, second = store.create(), store.create()
    assert first != second and UUID(first).version == 4 and len(first) == 32
    store.set_sets(first, [("A-1", 1), ("A-1", 2)])
    store.set_sets(second, [("B-1", 2)])
    store.add_adjustment(first, "p1", 1, -2)
    store.add_adjustment(first, "p1", 1, 3)
    store.set_sets(first, [("C-1", 1)])
    assert CollectionStore(path).get(first) == {"id": first, "sets": [("C-1", 1)],
                                                "adjustments": [("p1", 1, -2), ("p1", 1, 3)], "version": 2}
    assert store.get(second) == {"id": second, "sets": [("B-1", 2)], "adjustments": [], "version": 1}
    for bad in ([("A", 1)], [("A-1", 0)], [("A-1", -1)], [("A-1", True)], [("A-1", 1.5)]):
        with pytest.raises(ValueError):
            store.set_sets(first, [("A-1", 1)] + bad)
        assert store.get(first)["sets"] == [("C-1", 1)]
    with pytest.raises(ValueError):
        store.add_adjustment(first, "p1", 1, 1.5)
    for call in (lambda: store.get("missing"), lambda: store.set_sets("missing", []),
                 lambda: store.add_adjustment("missing", "p1", 1, 1)):
        with pytest.raises(KeyError):
            call()
    store.set_sets(first, [])
    assert store.get(first)["sets"] == []
