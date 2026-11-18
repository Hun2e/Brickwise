"""임시 SQLite로 결정적인 번호·지시어·기체 해석을 검증한다."""

import pytest

from rag_service.rag.schemas import RouterOutputV2
from rag_service.services.resolve import resolve_sets
from test_db_queries import add_set, conn


def output(route="fact", numbers=(), entities=(), refs=()):
    return RouterOutputV2(route=route, set_nums=list(numbers), entities=list(entities),
                          set_refs=list(refs), confidence=0.9)


@pytest.mark.parametrize("question,refs", [("이 세트는?", ()), ("그거 언제 나왔어?", ()), ("정보", ("selected",))])
def test_selected_reference(conn, question, refs):
    result = resolve_sets("fact", output(refs=refs), question, " A ", conn)
    assert result.set_nums == ["A-1"] and result.needs_input is None


def test_set_ops_completes_selected_and_deduplicates(conn):
    assert resolve_sets("set_ops", output(numbers=["B"]), "공통 부품", "A", conn).set_nums == ["B-1", "A-1"]
    result = resolve_sets("set_ops", output(numbers=["A", "A-1"]), "공통", "A", conn)
    assert result.set_nums == ["A-1"] and result.needs_input


@pytest.mark.parametrize("route", ["fact", "narrative", "compare", "set_ops"])
def test_unknown_subject_asks(conn, route):
    assert resolve_sets(route, output(route), "알려줘", None, conn).needs_input


def test_unknown_number_never_replaced_by_selected(conn):
    add_set(conn, "12345-1")
    result = resolve_sets("fact", output(numbers=["1234"]), "부품 수", "A", conn)
    assert result.needs_input and [r.set_num for r in result.candidates] == ["12345-1"]


def test_missing_selected_reference_does_not_silently_use_other_number(conn):
    assert resolve_sets("compare", output(numbers=["A"], refs=["selected"]), "비교", None, conn).needs_input


def test_build_without_target_is_recommendation(conn):
    result = resolve_sets("build", output("build"), "추천", None, conn)
    assert not result.set_nums and result.needs_input is None
    assert resolve_sets("build", output("build"), "만들기", "A", conn).set_nums == ["A-1"]


@pytest.fixture
def entities(conn):
    conn.execute("CREATE TABLE set_entities(set_num TEXT, wiki_title TEXT, kind TEXT)")
    conn.execute("CREATE TABLE ko_aliases(alias TEXT, canonical_en TEXT, kind TEXT)")
    for number, name, year, scope in [("100-1", "Falcon", 2019, "core"),
                                       ("101-1", "Falcon", 2017, "core"),
                                       ("102-1", "Falcon", 2010, "compare_only"),
                                       ("103-1", "X-wing", 2015, "core")]:
        add_set(conn, number, name, year, scope=scope)
        conn.execute("INSERT INTO set_entities VALUES (?, ?, 'vehicle')", (number, name))
    conn.execute("INSERT INTO ko_aliases VALUES ('팔콘', 'Falcon', 'vehicle')")
    return conn


def test_entity_uses_deterministic_core_representative(entities):
    result = resolve_sets("compare", output(entities=["팔콘"]), "팔콘 판본", None, entities)
    assert result.set_nums == ["101-1"] and result.needs_input is None
    assert resolve_sets("compare", output(entities=["Unknown"]), "Unknown", None, entities).needs_input


def test_multiple_entities_ask_instead_of_guessing(entities):
    result = resolve_sets("compare", output(), "Falcon과 X-wing 비교", None, entities)
    assert result.needs_input and len(result.candidates) == 2
