"""실제 SQLite 수량 회귀 및 pandas 독립 계산 비교."""

import random
import sqlite3
from collections import Counter
from contextlib import closing

import pandas as pd
import pytest

from rag_service.config import load_settings, resolve_path
from rag_service.planner import analyze, collection_have, recommend


@pytest.fixture(scope="module")
def real_conn():
    path = resolve_path(load_settings()["paths"]["sqlite"])
    if not path.is_file():
        pytest.skip("data/lego.sqlite가 없습니다.")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
        yield conn


def test_known_part_counts(real_conn):
    assert sum(collection_have(real_conn, [("75192-1", 2)]).values()) == 15082
    result = analyze(real_conn, Counter(), "75301-1")
    assert result.need_total == result.official_num_parts == 474
    assert not result.count_mismatch
    mismatch = analyze(real_conn, Counter(), "75257-1")
    assert (mismatch.official_num_parts, mismatch.need_total, mismatch.count_mismatch) == (1328, 1351, True)
    have = collection_have(real_conn, [("75192-1", 1)])
    own = analyze(real_conn, have, "75192-1")
    assert (own.total_missing, own.coverage, own.rows) == (0, 1.0, [])


@pytest.mark.parametrize("mode", ["exact", "ignore_color"])
def test_independent_pandas(real_conn, mode):
    numbers = [r[0] for r in real_conn.execute("SELECT set_num FROM set_scope WHERE scope='core' ORDER BY set_num")]
    selected = random.Random(20261021).sample(numbers, 8)
    owned, targets = selected[:3], selected[3:]
    have = collection_have(real_conn, [(number, 1) for number in owned])
    owned_frame = pd.read_sql_query(
        "SELECT part_num, color_id, quantity FROM set_parts_flat WHERE set_num IN (?, ?, ?)", real_conn, params=owned)
    keys = ["part_num", "color_id"] if mode == "exact" else ["part_num"]
    available = owned_frame.groupby(keys)["quantity"].sum().rename("have")
    for target in targets:
        needed = pd.read_sql_query(
            "SELECT part_num, color_id, quantity FROM set_parts_flat WHERE set_num=?", real_conn, params=[target])
        needed = needed.groupby(keys)["quantity"].sum().rename("need")
        joined = needed.to_frame().join(available).fillna(0)
        if mode == "exact":
            joined.loc[joined.index.get_level_values("color_id") == -1, "have"] = 0
        missing = (joined["need"] - joined["have"]).clip(lower=0)
        result = analyze(real_conn, have, target, mode)
        assert result.total_missing == int(missing.sum())
        assert result.lots_missing == int((missing > 0).sum())
        assert result.need_total == int(needed.sum())
    result = recommend(real_conn, have, set(owned), mode=mode, top_n=20)
    assert len(result) == 20
    assert not set(owned).intersection(r.set_info.set_num for r in result)
    assert result == sorted(result, key=lambda r: (r.total_missing, r.lots_missing, -r.coverage, r.set_info.year, r.set_info.set_num))
    for row in result:
        analysis = analyze(real_conn, have, row.set_info.set_num, mode)
        assert (row.total_missing, row.lots_missing, row.coverage) == (analysis.total_missing, analysis.lots_missing, analysis.coverage)
