"""로컬 SQLite만 사용하는 DB 질의·템플릿 확인 CLI."""

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rag_service.config import load_settings, resolve_path
from rag_service.db.queries import FilterSpec
from rag_service.rag.templates import (
    TemplateAnswer, answer_build_analysis, answer_build_recommend, answer_compare,
    answer_fact, answer_filter, answer_set_ops,
)


def _owned(value: str) -> tuple[str, int]:
    number, sep, qty = value.partition("=")
    try:
        count = int(qty) if sep else 1
        if not number or count < 1:
            raise ValueError
    except ValueError:
        raise argparse.ArgumentTypeError("보유 세트는 세트번호=양의수량 형식입니다.") from None
    return number, count


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--db", help="로컬 SQLite 경로 (기본: settings)")
    result.add_argument("--json", action="store_true", help="문장·카드·표·경고를 JSON으로 출력")
    commands = result.add_subparsers(dest="command", required=True)
    fact = commands.add_parser("fact", help="세트 개요")
    fact.add_argument("set_num")
    fact.add_argument("--question", default="")
    filtering = commands.add_parser("filter", help="조건 검색")
    for name in ("year-from", "year-to", "parts-min", "parts-max"):
        filtering.add_argument("--" + name, type=int)
    filtering.add_argument("--subtheme")
    filtering.add_argument("--minifig")
    filtering.add_argument("--has-minifig", action=argparse.BooleanOptionalAction, default=None)
    filtering.add_argument("--name-keyword", action="append", default=[])
    filtering.add_argument("--scope", action="append", choices=["core", "compare_only"])
    filtering.add_argument("--sort", choices=["year", "num_parts", "name", "set_num"], default=FilterSpec.sort)
    filtering.add_argument("--order", choices=["asc", "desc"], default=FilterSpec.order)
    filtering.add_argument("--limit", type=int, default=FilterSpec.limit)
    ops = commands.add_parser("setops", help="공통·단독 부품 비교")
    ops.add_argument("a")
    ops.add_argument("b")
    ops.add_argument("--mode", choices=["exact", "ignore_color"], default="exact")
    compare = commands.add_parser("compare", help="판본 비교")
    compare.add_argument("set_num")
    for command in ("build-analyze", "build-recommend"):
        build = commands.add_parser(command)
        build.add_argument("--owned", action="append", type=_owned, default=[], metavar="SET=QTY")
        build.add_argument("--mode", choices=["exact", "ignore_color"])
        if command == "build-analyze":
            build.add_argument("target")
        else:
            build.add_argument("--scope", action="append", choices=["core", "compare_only", "all"])
            for name in ("year-min", "year-max", "parts-min", "parts-max", "top-n"):
                build.add_argument("--" + name, type=int)
    return result


def _run(conn: sqlite3.Connection, args: argparse.Namespace, settings: dict) -> TemplateAnswer:
    if args.command == "fact":
        return answer_fact(conn, args.set_num, args.question)
    if args.command == "filter":
        spec = FilterSpec(args.year_from, args.year_to, args.parts_min, args.parts_max,
                          args.subtheme, args.minifig, args.has_minifig, tuple(args.name_keyword),
                          tuple(args.scope) if args.scope is not None else FilterSpec().scopes,
                          args.sort, args.order, args.limit)
        return answer_filter(conn, spec)
    if args.command == "setops":
        return answer_set_ops(conn, args.a, args.b, args.mode)
    if args.command == "compare":
        return answer_compare(conn, args.set_num)
    collection = {"sets": args.owned, "adjustments": []}
    mode = args.mode or settings["planner"]["default_mode"]
    if args.command == "build-analyze":
        return answer_build_analysis(conn, collection, args.target, mode)
    filters = {key: getattr(args, key) for key in
               ("scope", "year_min", "year_max", "parts_min", "parts_max", "top_n")
               if getattr(args, key) is not None}
    return answer_build_recommend(conn, collection, mode, filters)


def _cell(value: object, fmt: str | None = None) -> str:
    if fmt == "count" and type(value) is int:
        text = f"{value:,}"
    elif fmt == "percent" and type(value) in (int, float):
        text = f"{value:.1%}"
    else:
        text = "색상 무시" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", " ")


def render(result: TemplateAnswer) -> str:
    """PM 검수용으로 문장·Markdown 표·카드 원문을 출력한다."""
    lines = [result.answer]
    for table in result.tables:
        lines.extend(["", table["title"], "| " + " | ".join(table["columns"]) + " |",
                      "| " + " | ".join("---" for _ in table["columns"]) + " |"])
        formats = table.get("formats", {})
        lines.extend("| " + " | ".join(_cell(cell, formats.get(column))
                                       for column, cell in zip(table["columns"], row)) + " |"
                     for row in table["rows"])
    for warning in result.warnings:
        lines.append("주의: " + warning)
    for card in result.cards:
        lines.extend(["", f"[근거 {card.n}] {card.title}", card.text,
                      f"출처: {card.url} · {card.license}"])
    return "\n".join(lines)


def _configure_output() -> None:
    """Windows 콘솔과 리디렉션 모두 UTF-8로 쓰되 교체 스트림도 허용한다."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError, ValueError):
            pass


def main(argv: list[str] | None = None) -> int:
    _configure_output()
    args = parser().parse_args(argv)
    try:
        settings = load_settings()
        path = resolve_path(args.db or settings["paths"]["sqlite"])
        result = None
        if path.is_file():
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
                result = _run(conn, args, settings)
    except (OSError, ValueError, KeyError, sqlite3.Error):
        print("질의 실패: 세트 번호·조건·DB 적재 상태와 설정을 확인하세요.", file=sys.stderr)
        return 1
    if result is None:
        print("DB가 없습니다. Rebrickable 적재를 먼저 실행하세요.")
        return 1
    print(json.dumps(asdict(result), ensure_ascii=False, indent=2) if args.json else render(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
