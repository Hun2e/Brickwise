"""7경로 공통 응답을 확인하는 CLI. --history는 JSON 배열 파일 경로다."""

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def _owned(value: str) -> tuple[str, int]:
    number, sep, quantity = value.partition("=")
    try:
        if not sep or not number.strip() or int(quantity) < 1:
            raise ValueError
        return number.strip(), int(quantity)
    except ValueError:
        raise argparse.ArgumentTypeError("보유 세트는 SET=양의수량 형식으로 입력하세요.") from None


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("question", help="질문")
    result.add_argument("--set", dest="selected_set", help="선택 세트 번호")
    result.add_argument("--collection-set", action="append", type=_owned, default=[], metavar="SET=QTY",
                        help="보유 세트와 수량 (반복 가능, 이번 요청에만 사용)")
    result.add_argument("--history", type=Path, help="직전 대화를 포함한 JSON 배열 파일")
    return result


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = parser().parse_args(argv)
    # --help에서는 모델·설정·DB를 초기화하지 않는다.
    from rag_service.planner import SetNotFound, normalize_set_num
    from rag_service.llm import MissingApiKey
    from rag_service.services.orchestrator import answer, default_deps, log_exception

    try:
        history = json.loads(args.history.read_text(encoding="utf-8")) if args.history else []
        if not isinstance(history, list) or not all(isinstance(row, dict) for row in history):
            raise ValueError
        with default_deps() as deps:
            deps.collection = {"sets": [(normalize_set_num(deps.conn, n), qty)
                                         for n, qty in args.collection_set], "adjustments": []}
            response = answer(args.question, selected_set=args.selected_set, history=history, deps=deps,
                              on_event=lambda name, data: print(f"진행: {name}", file=sys.stderr))
        print(json.dumps(asdict(response), ensure_ascii=False, indent=2))
        return 0 if response.needs_input is None else 1
    except MissingApiKey:
        print("API 키가 설정되지 않았습니다", file=sys.stderr)
        return 1
    except (OSError, ValueError, SetNotFound):
        print("입력 파일·세트 번호·DB와 설정을 확인하세요.", file=sys.stderr)
        return 1
    except Exception as exc:
        log_exception(exc)
        print("처리 중 오류가 발생했습니다", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
