"""정제는 조건 교집합과 사용자가 본 순서를 보존해야 한다."""

from dataclasses import asdict, replace

import pytest

from rag_service.db.queries import FilterSpec, filter_sets
from rag_service.services.search import search_sets
from test_db_queries import add_set, conn
from test_search_offline import db, settings, model_for, ranked


def run(db, settings, question, conditions, **kwargs):
    return search_sets(question, conn=db, settings=settings, model=model_for(conditions),
                       meta_store=ranked(), **kwargs)


def test_empty_filter_allowlist_keeps_legacy_sql(db):
    spec = FilterSpec(parts_min=100)
    assert filter_sets(db, spec) == filter_sets(db, replace(spec, set_nums=()))
    rows, total = filter_sets(db, replace(spec, set_nums=("1003-1", "1001-1")))
    assert total == 2 and [r.set_num for r in rows] == ["1001-1", "1003-1"]
    assert filter_sets(db, replace(spec, set_nums=("' OR 1=1 --",))) == ([], 0)


def test_two_refinements_intersect_limits_and_keep_display_order(db, settings):
    add_set(db, "1005-1", "Other Speeder", 2021, 400)
    first = run(db, settings, "2016년 이후 찾아줘", {"year_from": 2016})
    displayed = ["1005-1", "1003-1", "1004-1", "1002-1"]
    second = run(db, settings, "그중 500개 이하만", {"parts_max": 500},
                 base_spec=asdict(first.spec), allow=displayed, keep_order=displayed)
    assert second.set_nums == ["1005-1", "1004-1"]
    third = run(db, settings, "그중 2021년에 나온 건?", {"year_from": 2021, "year_to": 2021},
                base_spec=second.spec, allow=second.set_nums, keep_order=True)
    assert third.set_nums == ["1005-1"] and third.spec.parts_max == 500
    assert third.spec.year_from == third.spec.year_to == 2021


def test_allow_empty_and_contradictory_conditions_never_expand(db, settings):
    assert run(db, settings, "그중 2020년 이후", {"year_from": 2020}, allow=[]).no_result
    result = run(db, settings, "그중 500개 이하", {"parts_max": 500},
                 base_spec=FilterSpec(parts_min=1000), allow=["1001-1", "1004-1"])
    assert result.no_result and result.spec.parts_min == 1000 and result.spec.parts_max == 500


def test_single_value_conditions_are_anded_not_replaced(db, settings):
    base = FilterSpec(has_minifig=False, name_keywords=("Falcon",))
    result = run(db, settings, "그중 미니피그 있는 세트", {"has_minifig": True}, base_spec=base)
    assert result.no_result
    assert result.spec.name_keywords == ("Falcon",)


def test_keep_order_precedes_result_limit(db, settings):
    order = ["1003-1", "1001-1", "1002-1"]
    settings["search"].update(result_limit=1, sql_candidate_limit=1)
    result = run(db, settings, "그중 2018년 이후 1개", {"year_from": 2018, "limit": 1},
                 allow=order, keep_order=order)
    assert result.set_nums == order[:1]
    assert "이전 표시 순서" in result.template.cards[0].text


def test_looser_followup_does_not_drop_previous_exclusions(db, settings):
    result = run(db, settings, "그중 100개 이상", {"parts_min": 100},
                 base_spec=FilterSpec(parts_min=1000, exclude_keywords=("Falcon",)))
    assert result.spec.parts_min == 1000 and result.spec.exclude_keywords == ("Falcon",)
    assert result.set_nums == ["1002-1", "1003-1"]
