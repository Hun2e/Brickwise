"""2026-10-21 실데이터의 전체 적재 및 core 집계 회귀 검증."""

from contextlib import closing

import pytest

from rag_service.config import load_settings, resolve_path
from rag_service.data_prep.rebrickable_loader import build_derived, load
from rag_service.data_prep.scope import select_scope
from rag_service.db.connection import connect
from rag_service.db.integrity import run_checks


@pytest.fixture(scope="module")
def real_database(tmp_path_factory):
    settings = load_settings()
    csv_dir = resolve_path(settings["rebrickable"]["csv_dir"])
    # 공개본에는 원본 CSV가 없다. scripts/load_rebrickable.py로 내려받은 뒤에만 검증한다.
    if not any(csv_dir.glob("*.csv.gz")):
        pytest.skip("Rebrickable 원본 CSV가 없습니다.")
    path = tmp_path_factory.mktemp("real_rebrickable") / "lego.sqlite"
    report = load(csv_dir, path)
    with closing(connect(path)) as conn:
        derived = build_derived(conn)
        scope = select_scope(conn, settings["scope"])
        yield conn, report, derived, scope


def test_real_counts_and_integrity(real_database):
    conn, report, derived, scope = real_database
    assert len(report.row_counts) == 12
    assert report.row_counts == report.csv_counts
    assert derived["set_root_theme"] == report.row_counts["sets"]
    assert derived["set_parts_flat"] > 0
    assert scope["core"] == 332
    assert scope["compare_only"] > 0
    checks = {check.name: check for check in run_checks(conn, report.csv_counts)}
    assert checks["set_parts_flat_pk"].ok
    assert all(check.ok for check in checks.values() if check.name != "core_num_parts"), checks


def test_real_num_parts_reproduction(real_database):
    conn, _, _, _ = real_database
    matched = conn.execute("""
        SELECT COUNT(*) FROM (
            SELECT s.set_num FROM set_scope c JOIN sets s USING (set_num)
            LEFT JOIN set_parts_flat p USING (set_num)
            WHERE c.scope='core' GROUP BY s.set_num, s.num_parts
            HAVING COALESCE(SUM(p.quantity), 0) = s.num_parts
        )
    """).fetchone()[0]
    assert matched >= 331
