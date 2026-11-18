"""소형 CSV로 집계, 무결성, 재실행, 네트워크 가드를 결정적으로 검증한다."""

import csv
import gzip
import io
import runpy
import shutil
import sys
from contextlib import closing
from datetime import datetime, timezone
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError

import pytest

from rag_service.config import load_settings, project_root
from rag_service import config
from rag_service.data_prep import rebrickable_download as download
from rag_service.data_prep import rebrickable_loader as loader
from rag_service.data_prep.scope import select_scope
from rag_service.db.connection import connect
from rag_service.db.integrity import run_checks

CSV_DIR = project_root() / "tests/fixtures/rebrickable_mini"
SCOPE = {
    "root_theme": "Star Wars", "year_min": 2015, "year_max": 2026,
    "parts_min": 2, "parts_max": 10,
    "compare_keywords": ["Millennium Falcon", "X-wing", "TIE"], "compare_parts_min": 2,
}


@pytest.fixture
def database(tmp_path, monkeypatch):
    settings = load_settings()
    settings["rebrickable"]["batch_size"] = 2
    monkeypatch.setattr(loader, "load_settings", lambda: settings)
    path = tmp_path / "mini.sqlite"
    report = loader.load(CSV_DIR, path)
    with closing(connect(path)) as conn:
        loader.build_derived(conn)
        select_scope(conn, SCOPE)
        yield conn, report


def parts(conn, set_num):
    return {(row[0], row[1]): row[2] for row in conn.execute(
        "SELECT part_num, color_id, quantity FROM set_parts_flat WHERE set_num = ?", (set_num,)
    )}


def test_aggregation(database):
    conn, _ = database
    assert parts(conn, "A-1") == {("p1", 1): 2, ("p2", 1): 3, ("p1", 2): 2}
    assert parts(conn, "B-1") == {("p2", 2): 5}
    assert parts(conn, "C-1") == {("p1", 1): 5, ("p2", 2): 2}
    assert parts(conn, "D-1") == {}
    assert parts(conn, "G-1") == {("p1", 1): 3}  # 범위 밖 세트도 집계한다.
    assert parts(conn, "fig-1") == {}
    assert tuple(conn.execute("SELECT * FROM set_root_theme WHERE set_num='A-1'").fetchone()) == (
        "A-1", 1, "Star Wars", "Vehicles",
    )


def test_csv_counts_and_types(database):
    conn, report = database
    expected = {}
    for table in download.TABLES:
        with gzip.open(CSV_DIR / f"{table}.csv.gz", "rt", encoding="utf-8", newline="") as stream:
            expected[table] = sum(1 for _ in csv.reader(stream)) - 1
    assert report.row_counts == report.csv_counts == expected
    assert report.seconds > 0 and report.db_bytes > 0
    assert {tuple(row) for row in conn.execute("SELECT DISTINCT is_spare, typeof(is_spare) FROM inventory_parts")} == {(0, "integer"), (1, "integer")}
    assert [tuple(row) for row in conn.execute("SELECT id, is_trans FROM colors ORDER BY id")] == [(1, 0), (2, 1)]
    assert tuple(conn.execute("SELECT element_id, design_id FROM elements").fetchone()) == ("0001", "001")
    assert conn.execute("SELECT y1 FROM colors WHERE id=2").fetchone()[0] is None


def test_scope(database):
    conn, _ = database
    assert select_scope(conn, SCOPE) == {"core": 3, "compare_only": 2}
    assert dict(conn.execute("SELECT set_num, scope FROM set_scope")) == {
        "A-1": "core", "B-1": "core", "C-1": "core", "E-1": "compare_only", "I-1": "compare_only",
    }
    assert select_scope(conn, {**SCOPE, "compare_keywords": []}) == {"core": 3, "compare_only": 0}
    assert select_scope(conn, {**SCOPE, "compare_keywords": ["%", "_"]})["compare_only"] == 0


def test_integrity_detects_mismatch_and_csv_count(database):
    conn, report = database
    checks = {check.name: check for check in run_checks(conn, report.csv_counts)}
    assert [check.name for check in checks.values() if not check.ok] == ["core_num_parts"]
    assert "matched=2/3" in checks["core_num_parts"].detail
    assert "('B-1', 6, 5)" in checks["core_num_parts"].detail
    broken_counts = {**report.csv_counts, "sets": 0}
    assert not next(c for c in run_checks(conn, broken_counts) if c.name == "row_count_sets").ok


def test_integrity_fk_diagnostics(database):
    conn, report = database
    conn.executemany("INSERT INTO inventory_parts VALUES (?, ?, ?, ?, ?, ?)", [
        (999 + i, "missing", 999, 1, 0, "") for i in range(7)
    ])
    checks = {check.name: check for check in run_checks(conn)}
    inventory = checks["fk_inventory_parts_inventory_id"]
    assert not inventory.ok and "violations=7" in inventory.detail
    assert inventory.detail.count("'missing'") == 5
    for key in ("part_num", "color_id"):
        check = checks[f"fk_inventory_parts_{key}"]
        assert check.ok and "violations=7" in check.detail


def test_idempotency(tmp_path):
    path = tmp_path / "repeat.sqlite"
    snapshots, reports = [], []
    for _ in range(2):
        reports.append(loader.load(CSV_DIR, path))
        with closing(connect(path)) as conn:
            derived = loader.build_derived(conn)
            selected = select_scope(conn, SCOPE)
            snapshot = [tuple(row) for row in conn.execute("SELECT * FROM set_parts_flat ORDER BY set_num, part_num, color_id")]
            assert loader.build_derived(conn) == derived
            assert select_scope(conn, SCOPE) == selected
            snapshots.append(snapshot)
    assert reports[0].row_counts == reports[1].row_counts
    assert reports[0].csv_counts == reports[1].csv_counts
    assert snapshots[0] == snapshots[1]


@pytest.mark.parametrize("problem", ["header", "boolean", "missing", "truncated"])
def test_bad_csv_preserves_database(tmp_path, problem):
    path = tmp_path / "existing.sqlite"
    loader.load(CSV_DIR, path)
    before = path.read_bytes()
    csv_dir = tmp_path / "csv"
    shutil.copytree(CSV_DIR, csv_dir)
    target = csv_dir / "inventory_parts.csv.gz"
    if problem == "missing":
        target.unlink()
    elif problem == "truncated":
        target.write_bytes(target.read_bytes()[:-8])
    else:
        with gzip.open(target, "rt", encoding="utf-8") as stream:
            text = stream.read()
        text = text.replace("inventory_id", "wrong_id", 1) if problem == "header" else text.replace("False", "invalid", 1)
        with gzip.open(target, "wt", encoding="utf-8") as stream:
            stream.write(text)
    with pytest.raises((ValueError, OSError, EOFError)):
        loader.load(csv_dir, path)
    assert path.read_bytes() == before
    assert not list(tmp_path.glob("existing.sqlite.*.tmp"))


def test_latest_tie_and_empty_inventory(database):
    conn, _ = database
    conn.execute("INSERT INTO inventories VALUES (22, 2, 'B-1')")
    loader.build_derived(conn)
    assert parts(conn, "B-1") == {}  # 최신 판본에 부품이 없어도 과거 판본으로 대체하지 않는다.


def test_minifig_only_set_and_inclusive_parts_bounds(database):
    conn, _ = database
    conn.execute("DELETE FROM inventory_parts WHERE inventory_id=30")
    loader.build_derived(conn)
    assert parts(conn, "C-1") == {("p1", 1): 4, ("p2", 2): 2}
    assert select_scope(conn, {**SCOPE, "parts_min": 7, "parts_max": 7})["core"] == 2


def test_invalid_theme_rolls_back(database):
    conn, _ = database
    before = [tuple(row) for row in conn.execute("SELECT * FROM set_root_theme")]
    with conn:
        conn.execute("UPDATE themes SET parent_id=3 WHERE id=1")
    with pytest.raises(ValueError, match="테마"):
        loader.build_derived(conn)
    assert [tuple(row) for row in conn.execute("SELECT * FROM set_root_theme")] == before


@pytest.mark.parametrize(("stamp", "now", "expected"), [
    (None, "2026-10-21T00:00:00+00:00", True),
    ("2026-10-21T04:09Z", "2026-10-21T23:59:59+00:00", False),
    ("2026-10-21T23:59:59Z", "2026-10-22T00:00:00+00:00", True),
    ("2026-10-21T23:59:59Z", "2026-10-22T08:59:59+09:00", False),
    ("2026-10-22T00:00:00Z", "2026-10-21T00:00:00+00:00", False),
])
def test_daily_guard(tmp_path, stamp, now, expected):
    if stamp:
        (tmp_path / "DOWNLOADED_AT.txt").write_text(stamp, encoding="utf-8")
    assert download.should_download(tmp_path, datetime.fromisoformat(now)) is expected


def test_invalid_download_marker_fails_closed(tmp_path):
    (tmp_path / "DOWNLOADED_AT.txt").write_text("invalid", encoding="utf-8")
    with pytest.raises(ValueError):
        download.should_download(tmp_path, datetime.now(timezone.utc))


@pytest.fixture
def network_stub(monkeypatch):
    calls, sleeps = [], []
    payload = gzip.compress(b"header\nvalue\n", mtime=0)

    def fake_open(request, timeout):
        calls.append(request)
        return io.BytesIO(payload)

    monkeypatch.setattr(download, "urlopen", fake_open)
    monkeypatch.setattr(download.time, "sleep", sleeps.append)
    return calls, sleeps, payload


def test_download_success_and_repeated_call(tmp_path, network_stub):
    calls, sleeps, payload = network_stub
    sizes = download.download_all(tmp_path, "https://example.invalid/", "Test/1 (test@example.invalid) python-requests", 0.5)
    assert sizes == dict.fromkeys(download.TABLES, len(payload))
    assert len(calls) == 12 and sleeps == [0.5] * 11
    assert calls[0].get_header("User-agent").startswith("Test/1")
    assert all((tmp_path / f"{name}.csv.gz").read_bytes() == payload for name in download.TABLES)
    assert download.download_all(tmp_path, "https://example.invalid/", "Test/1", 0.5) == {}
    assert len(calls) == 12
    assert not (tmp_path / ".download.lock").exists()


@pytest.mark.parametrize("status", [429, 503, 404])
def test_download_http_failure_preserves_files_and_guards_retry(tmp_path, monkeypatch, network_stub, status):
    _, sleeps, _ = network_stub
    attempts = []
    original = tmp_path / "themes.csv.gz"
    original.write_bytes(b"original")
    headers = Message()
    headers["Retry-After"] = "7"

    def failing_open(request, timeout):
        attempts.append(request)
        raise HTTPError(request.full_url, status, "failure", headers, None)

    monkeypatch.setattr(download, "urlopen", failing_open)
    with pytest.raises(HTTPError):
        download.download_all(tmp_path, "https://example.invalid", "Test/1", 0.5)
    retries = load_settings()["external_api"]["max_retries"] if status != 404 else 0
    assert len(attempts) == retries + 1
    assert sleeps == [7] * retries
    assert original.read_bytes() == b"original"
    assert not (tmp_path / "DOWNLOADED_AT.txt").exists()
    assert not (tmp_path / ".download.lock").exists()
    assert download.download_all(tmp_path, "https://example.invalid", "Test/1", 0.5) == {}


def test_download_rejects_bad_gzip(tmp_path, monkeypatch, network_stub):
    monkeypatch.setattr(download, "urlopen", lambda *args, **kwargs: io.BytesIO(b"not gzip"))
    with pytest.raises(gzip.BadGzipFile):
        download.download_all(tmp_path, "https://example.invalid", "Test/1", 0.5)
    assert not list(tmp_path.glob("*.csv.gz"))


def test_download_recovers_after_retry(tmp_path, monkeypatch, network_stub):
    _, sleeps, payload = network_stub
    requests = []
    headers = Message()
    headers["Retry-After"] = "2"

    def retry_once(request, timeout):
        requests.append(request.full_url)
        if len(requests) == 1:
            raise HTTPError(request.full_url, 429, "retry", headers, None)
        return io.BytesIO(payload)

    monkeypatch.setattr(download, "urlopen", retry_once)
    assert len(download.download_all(tmp_path, "https://example.invalid", "Test/1", 0.5)) == 12
    assert len(requests) == 13 and requests[0] == requests[1]
    assert sleeps == [2] + [0.5] * 11


@pytest.mark.parametrize("broken", [False, True])
def test_cli_offline_report_and_errors(tmp_path, monkeypatch, capsys, broken):
    settings = load_settings()
    settings["rebrickable"]["csv_dir"] = str(tmp_path / "missing" if broken else CSV_DIR)
    settings["paths"]["sqlite"] = str(tmp_path / "cli.sqlite")
    settings["scope"] = SCOPE
    monkeypatch.setattr(config, "load_settings", lambda: settings)

    def unexpected_network(*args, **kwargs):
        pytest.fail("--skip-download must not inspect or access downloads")

    monkeypatch.setattr(download, "should_download", unexpected_network)
    monkeypatch.setattr(download, "urlopen", unexpected_network)
    monkeypatch.setattr(sys, "argv", ["load_rebrickable.py", "--skip-download"])
    with pytest.raises(SystemExit) as result:
        runpy.run_path(str(project_root() / "scripts/load_rebrickable.py"), run_name="__main__")
    assert result.value.code == int(broken)
    output = capsys.readouterr()
    if broken:
        assert "적재 실패" in output.err and "Traceback" not in output.err
    else:
        assert "[WARN] core_num_parts: matched=2/3" in output.out
        assert "Derived rows:" in output.out and "DB:" in output.out
        assert output.out.count("[PASS] row_count_") == 12
