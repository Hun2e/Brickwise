"""Rebrickable 다운로드 가드 → 적재 → 파생 → 범위 → 진단 CLI."""

import argparse
import csv
import os
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rag_service.config import load_settings, resolve_path
from rag_service.data_prep.rebrickable_download import download_all, should_download
from rag_service.data_prep.rebrickable_loader import build_derived, load
from rag_service.data_prep.scope import select_scope
from rag_service.db.connection import connect
from rag_service.db.integrity import run_checks


def main() -> int:
    """구조적 오류는 실패 종료, 원본 num_parts 불일치는 경고로 보고한다."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-download", action="store_true", help="네트워크 요청 없이 기존 CSV로 적재")
    args = parser.parse_args()
    settings = load_settings()
    csv_dir = resolve_path(settings["rebrickable"]["csv_dir"])
    sqlite_path = resolve_path(settings["paths"]["sqlite"])
    started = perf_counter()
    if not args.skip_download and should_download(csv_dir, datetime.now(timezone.utc)):
        contact = os.environ.get("WIKI_CONTACT", "")
        if not contact:
            print("WIKI_CONTACT 환경변수를 설정하거나 --skip-download로 기존 CSV를 사용하세요.")
            return 1
        app = settings["app"]
        sizes = download_all(
            csv_dir, settings["rebrickable"]["base_url"],
            f"{app['name']}/{app['version']} ({contact}) python-requests",
            settings["external_api"]["request_interval_sec"],
        )
        print(f"Downloaded bytes: {sizes}")
    report = load(csv_dir, sqlite_path)
    with closing(connect(sqlite_path)) as conn:
        derived = build_derived(conn)
        scope = select_scope(conn, settings["scope"])
        checks = run_checks(conn, report.csv_counts)
    print("Source: Rebrickable (https://rebrickable.com/downloads/)")
    print(f"Derived rows: {derived}")
    print(f"Scope: {scope}")
    for check in checks:
        status = "PASS" if check.ok else ("WARN" if check.name == "core_num_parts" else "FAIL")
        print(f"[{status}] {check.name}: {check.detail}")
    print(f"Load: {report.seconds:.2f} seconds; total: {perf_counter() - started:.2f} seconds")
    print(f"DB: {sqlite_path.stat().st_size / (1024 * 1024):.2f} MB")
    return int(any(not check.ok and check.name != "core_num_parts" for check in checks))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, EOFError, ValueError, KeyError, TypeError, csv.Error, sqlite3.Error):
        print("적재 실패: CSV 12종·설정 형식·DB 사용 여부를 확인하고 다시 실행하세요.", file=sys.stderr)
        raise SystemExit(1) from None
