"""근거 수치·구조화 표·예외 안내 및 오프라인 CLI 계약."""

import io
import json
import os
import re
import runpy
import subprocess
import sys
from dataclasses import replace

import pytest

from rag_service.config import load_settings, project_root
from rag_service.db.queries import FilterSpec, SetFact, compare_versions, filter_sets, set_fact, set_ops
from rag_service.planner import analyze, collection_have, recommend
from rag_service.rag.db_cards import (
    analysis_card, compare_card, fact_card, filter_card, recommend_card, set_ops_card,
)
from rag_service.rag.templates import (
    answer_build_analysis, answer_build_recommend, answer_compare, answer_fact, answer_filter, answer_set_ops,
)
from test_db_queries import conn, real_conn  # noqa: F401 - 두 테스트 파일이 같은 DB 픽스처를 공유한다.


def assert_evidence(result):
    assert [card.n for card in result.cards] == list(range(1, len(result.cards) + 1))
    assert "[근거 1]" in result.answer
    text = "\n".join(card.text for card in result.cards)
    answer = re.sub(r"\[근거 \d+\]", "", result.answer)
    for number in re.findall(r"\d[\d,]*(?:\.\d+)?", answer):
        assert number in text
    for card in result.cards:
        assert card.source_type == "db" and card.license == "Rebrickable"
        assert card.url.startswith("https://rebrickable.com/")
    for table in result.tables:
        assert set(table) == {"title", "columns", "rows", "formats"}
        assert set(table["formats"]) <= set(table["columns"])
        assert all(len(row) == len(table["columns"]) for row in table["rows"])


def test_fact_card_format_and_metadata():
    f = SetFact("75192-1", "Falcon", 2017, 7541, 7542, "UCS", "compare_only", [("Han Solo", 1200)], [])
    card = fact_card(f, 3)
    assert card.n == 3 and card.item_id == "75192-1"
    assert card.url == "https://rebrickable.com/sets/75192-1/"
    assert "부품 수(Rebrickable 기준): 7,541개 (부품 목록 집계 7,542개 — 원본 불일치)" in card.text
    assert "출시 연도: 2017" in card.text
    assert "미니피그(1,200): Han Solo × 1,200" in card.text
    assert "원본 불일치" not in fact_card(replace(f, agg_parts=7541), 1).text


@pytest.mark.parametrize("question, first", [
    ("부품 몇 개", "부품 수(Rebrickable 기준)"), ("언제 출시", "출시 연도"),
    ("미니피그 누가 들어", "미니피그("), ("소개해 줘", "부품 수(Rebrickable 기준)"),
])
def test_fact_requested_attributes_and_overview(conn, question, first):
    result = answer_fact(conn, "A", question)
    assert result.answer.split(": ", 1)[1].startswith(first)
    for label in ("부품 수(Rebrickable 기준)", "출시 연도", "미니피그(", "서브테마"):
        assert (label in result.answer) == (question == "소개해 줘" or label == first)
    assert_evidence(result)


@pytest.mark.parametrize("mode", ["exact", "ignore_color"])
def test_build_and_setops_templates(conn, mode):
    collection = {"sets": [("A", 1)], "adjustments": [("p1", 1, -1)]}
    before = {"sets": list(collection["sets"]), "adjustments": list(collection["adjustments"])}
    have = collection_have(conn, **collection)
    result = answer_build_analysis(conn, collection, "B", mode)
    expected = analyze(conn, have, "B", mode)
    assert_evidence(result)
    expected_card = analysis_card(expected, 1)
    if mode == "ignore_color":
        exact = analyze(conn, have, "B", "exact")
        expected_card = replace(expected_card, text=expected_card.text +
                                f"\n정확 일치 부족 수량: {exact.total_missing:,}개")
    assert result.cards == [expected_card]
    assert len(result.tables[0]["rows"]) == len(expected.rows)
    assert result.warnings and "원본 불일치" in result.cards[0].text
    recs = answer_build_recommend(conn, collection, mode, {"top_n": 2})
    expected_recs = recommend(conn, have, {"A-1"}, mode=mode, top_n=2)
    assert recs.cards == [recommend_card(expected_recs, mode, 1)]
    assert_evidence(recs)
    assert all(row[0] != "A-1" for row in recs.tables[0]["rows"])
    ops = answer_set_ops(conn, "A", "B", mode)
    assert ops.cards == [set_ops_card(set_ops(conn, "A", "B", mode), 1)]
    assert ops.cards[0].item_id == "A-1,B-1"
    assert ops.cards[0].url.endswith("/A-1/")
    assert_evidence(ops)
    assert collection == before


def test_compare_filter_empty_results_and_caps(conn, monkeypatch):
    comparison = answer_compare(conn, "A")
    assert comparison.cards == [compare_card(compare_versions(conn, "A", load_settings()), 1)]
    assert comparison.warnings
    assert_evidence(comparison)
    spec = FilterSpec(limit=1)
    facts, total = filter_sets(conn, spec)
    result = answer_filter(conn, spec)
    assert result.cards == [filter_card(facts, total, spec, 1)]
    assert_evidence(result)
    for result in (answer_filter(conn, FilterSpec(year_from=9999)),
                   answer_build_recommend(conn, {"sets": [("A", 1)]}, "exact", {"year_min": 9999})):
        assert result.tables[0]["rows"] == []
        assert_evidence(result)
    settings = load_settings()
    settings["compare"]["table_max_rows"] = 1
    monkeypatch.setattr("rag_service.rag.templates.load_settings", lambda: settings)
    result = answer_set_ops(conn, "A", "A")
    assert len(result.tables[0]["rows"]) == 1
    assert any("전체 3행 중 1행" in warning for warning in result.warnings)
    assert "공통 종류: 3종" in result.cards[0].text


def test_errors_have_safe_messages_and_candidates(conn):
    collection = {"sets": [("A", 1)]}
    for result in (answer_fact(conn, "missing", ""), answer_set_ops(conn, "A", "missing"),
                   answer_compare(conn, "missing"), answer_build_analysis(conn, collection, "missing", "exact")):
        assert "데이터에 없는 세트 번호입니다" in result.answer
        assert not result.cards and "Traceback" not in result.answer
    candidate = answer_fact(conn, "Millennium Falcon", "")
    assert candidate.tables[0]["rows"]
    for result in (answer_set_ops(conn, "A", "D"),
                   answer_build_analysis(conn, collection, "D", "exact"),
                   answer_build_recommend(conn, {"sets": [("D", 1)]}, "exact", {})):
        assert "부품 목록이 없는 세트입니다" in result.answer and not result.cards
    for result in (answer_build_analysis(conn, {}, "A", "exact"),
                   answer_build_recommend(conn, {}, "exact", {})):
        assert "등록하세요" in result.answer and not result.cards
    result = answer_build_analysis(conn, {"adjustments": [("p1", 1, 1)]}, "A", "exact")
    assert_evidence(result)


def test_real_templates_numeric_evidence(real_conn):
    collection = {"sets": [("75192-1", 1)]}
    results = [answer_fact(real_conn, "75192-1", "부품 몇 개"),
               answer_fact(real_conn, "75257-1", "출시"),
               answer_set_ops(real_conn, "75257-1", "75375-1"),
               answer_compare(real_conn, "75257-1"),
               answer_filter(real_conn, FilterSpec(subtheme="Ultimate Collector Series",
                                                    scopes=("core", "compare_only"))),
               answer_build_analysis(real_conn, collection, "75257-1", "exact"),
               answer_build_recommend(real_conn, collection, "exact", {"top_n": 5})]
    for result in results:
        assert_evidence(result)
        displayed = json.dumps([result.answer, result.tables, result.warnings], ensure_ascii=False)
        assert "공식 부품 수" not in displayed
        assert all("부품 수(Rebrickable 기준)" in warning
                   for warning in result.warnings if "원본 불일치" in warning)
    assert "부품 수(Rebrickable 기준)" in results[0].answer
    assert "부품 수(Rebrickable 기준)" in results[3].tables[0]["columns"]
    assert "7,541" in results[0].answer
    assert "1,328" in results[1].cards[0].text and "1,351" in results[1].cards[0].text
    assert "79종, 196개" in results[2].answer and "100종" in results[2].answer
    assert len(results[3].tables[0]["rows"]) == 10
    assert len(results[-1].tables[0]["rows"]) == 5


def run_cli(args, encoding="utf-8"):
    env = dict(os.environ, PYTHONIOENCODING=encoding, PYTHON_DOTENV_DISABLED="1")
    env.pop("GMS_KEY", None)
    code = """import sys, runpy
from pathlib import Path
def audit(event, args):
    if event in ('socket.connect', 'socket.getaddrinfo'):
        raise AssertionError('network forbidden')
    if event == 'open' and isinstance(args[0], (str, bytes)) and Path(args[0]).name == '.env':
        raise AssertionError('dotenv forbidden')
sys.addaudithook(audit)
sys.argv = ['scripts/db_query.py'] + sys.argv[1:]
runpy.run_path('scripts/db_query.py', run_name='__main__')
"""
    return subprocess.run([sys.executable, "-c", code, *args], cwd=project_root(), env=env,
                          capture_output=True, text=True, encoding="utf-8", timeout=60)


def test_cli_help_offline():
    result = run_cli(["--help"])
    assert result.returncode == 0
    for command in ("fact", "filter", "setops", "compare", "build-analyze", "build-recommend"):
        assert command in result.stdout


@pytest.mark.parametrize("args", [
    ["fact", "A"], ["filter", "--has-minifig"], ["setops", "A", "B"],
    ["compare", "A"], ["build-analyze", "B", "--owned", "A=1"],
    ["build-recommend", "--owned", "A=1", "--top-n", "2"],
])
def test_cli_subcommands_readonly_offline(conn, args):
    conn.commit()
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    result = run_cli(["--db", path, "--json", *args])
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert set(payload) == {"answer", "cards", "tables", "warnings", "items"}
    assert payload["cards"][0]["source_type"] == "db"


def test_cli_missing_db_and_bad_input(tmp_path):
    result = run_cli(["--db", str(tmp_path / "absent.sqlite"), "fact", "75192-1"])
    assert result.returncode == 1 and "DB가 없습니다" in result.stdout
    assert not (tmp_path / "absent.sqlite").exists()
    result = run_cli(["build-recommend", "--owned", "75192-1=0"])
    assert result.returncode == 2 and "Traceback" not in result.stderr


def test_year_count_and_percent_formats(real_conn):
    cli = runpy.run_path(str(project_root() / "scripts/db_query.py"))
    spec = FilterSpec(year_from=2000, year_to=2026, parts_min=1000, parts_max=8000,
                      subtheme="Ultimate Collector Series")
    filtered = answer_filter(real_conn, spec)
    assert "연도 하한: 2000" in filtered.cards[0].text
    assert "연도 상한: 2026" in filtered.cards[0].text
    assert "부품 수 하한: 1,000" in filtered.cards[0].text
    assert "부품 수 상한: 8,000" in filtered.cards[0].text
    results = [answer_fact(real_conn, "75192-1", "출시"), filtered,
               answer_compare(real_conn, "75257-1"),
               answer_set_ops(real_conn, "75257-1", "75375-1"),
               answer_build_recommend(real_conn, {"sets": [("75192-1", 1)]}, "exact", {"top_n": 5})]
    for result in results:
        rendered = cli["render"](result)
        assert not re.search(r"\b2,0\d{2}\b", rendered)
        assert_evidence(result)
        for table in result.tables:
            for column, fmt in table["formats"].items():
                values = [row[table["columns"].index(column)] for row in table["rows"]]
                if fmt in ("year", "count"):
                    assert all(type(value) is int for value in values)
                elif fmt == "percent":
                    assert all(type(value) is float and 0 <= value <= 1 for value in values)
                    assert all(f"{value:.1%}" in rendered for value in values)
    comparison_text = cli["render"](results[2])
    assert "| 75192-1 | Millennium Falcon | 2017 | 7,541 |" in comparison_text
    assert "부품 수(Rebrickable 기준): 7,541개" in comparison_text
    # 열 이름에 의존하지 않고 전달된 formats로 숫자와 식별자를 구분한다.
    custom = replace(results[0], answer="", cards=[], warnings=[], tables=[{
        "title": "표시 계약", "columns": ["A", "B", "C", "ID"],
        "rows": [[2017, 7541, 0.52, 2017]],
        "formats": {"A": "year", "B": "count", "C": "percent"},
    }])
    assert "| 2017 | 7,541 | 52.0% | 2017 |" in cli["render"](custom)


@pytest.mark.parametrize("args", [
    ["setops", "75257-1", "75375-1"], ["compare", "75257-1"],
    ["build-analyze", "75257-1", "--owned", "75192-1=1"],
])
def test_cli_cp949_streams_produce_utf8(real_conn, args):
    path = real_conn.execute("PRAGMA database_list").fetchone()[2]
    result = run_cli(["--db", path, *args], encoding="cp949")
    assert result.returncode == 0, result.stderr
    assert "[근거 1]" in result.stdout and "— 원본 불일치" in result.stdout
    assert "질의 실패" not in result.stdout + result.stderr


def test_cli_cp949_stderr_and_help(tmp_path):
    path = tmp_path / "invalid.sqlite"
    path.write_bytes(b"invalid database")
    result = run_cli(["--db", str(path), "fact", "A"], encoding="cp949")
    assert result.returncode == 1 and "질의 실패" in result.stderr
    assert "Traceback" not in result.stderr
    result = run_cli(["--help"], encoding="cp949")
    assert result.returncode == 0 and "로컬 SQLite" in result.stdout


def test_cli_stream_fallback_and_output_error_is_not_query_error(conn, monkeypatch):
    cli = runpy.run_path(str(project_root() / "scripts/db_query.py"))
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    conn.commit()

    class NoReconfigure(io.StringIO):
        def reconfigure(self, **kwargs):
            raise OSError("stream cannot be reconfigured")

    output, errors = NoReconfigure(), io.StringIO()
    with monkeypatch.context() as patch:
        patch.setattr(sys, "stdout", output)
        patch.setattr(sys, "stderr", errors)
        assert cli["main"](["--db", path, "fact", "A"]) == 0
    assert "[근거 1]" in output.getvalue() and not errors.getvalue()

    class FailingOutput(NoReconfigure):
        def write(self, value):
            raise UnicodeEncodeError("cp949", "—", 0, 1, "test output failure")

    with monkeypatch.context() as patch:
        patch.setattr(sys, "stdout", FailingOutput())
        patch.setattr(sys, "stderr", errors)
        with pytest.raises(UnicodeEncodeError):
            cli["main"](["--db", path, "fact", "A"])
    assert "질의 실패" not in errors.getvalue()


def test_cli_default_scopes_use_settings(conn, monkeypatch):
    cli = runpy.run_path(str(project_root() / "scripts/db_query.py"))
    settings = load_settings()
    settings["search"]["default_scopes"] = ["compare_only"]
    monkeypatch.setattr("rag_service.db.queries.load_settings", lambda: settings)
    args = cli["parser"]().parse_args(["filter"])
    result = cli["_run"](conn, args, settings)
    expected = answer_filter(conn, FilterSpec(scopes=("compare_only",)))
    assert result == expected and result.tables[0]["rows"]
    args = cli["parser"]().parse_args(["filter", "--scope", "core"])
    assert cli["_run"](conn, args, settings) == answer_filter(conn, FilterSpec(scopes=("core",)))


def test_cli_default_ucs_sixteen(real_conn):
    path = real_conn.execute("PRAGMA database_list").fetchone()[2]
    result = run_cli(["--db", path, "--json", "filter", "--subtheme", "Ultimate Collector Series"])
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert len(payload["tables"][0]["rows"]) == 16
    assert "16개" in payload["answer"]
