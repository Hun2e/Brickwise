"""라우팅 문항의 정확도·혼동 행렬·오답을 기록한다."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rag_service.config import resolve_path
from rag_service.eval.common import configured, digest, guarded, parser, write_result
from rag_service.eval.routing import evaluate, markdown, predict
from rag_service.eval.runtime import dependencies
from rag_service.eval.schema import read_cases


def main(argv: list[str] | None = None) -> int:
    cli = parser(__doc__)
    cli.add_argument("--file")
    cli.add_argument("--name", default="routing")
    cli.add_argument("--validate-only", action="store_true", help="키 없이 파일 형식만 검증")
    args = cli.parse_args(argv)

    def run() -> int:
        settings = configured(args)
        if args.validate_only:
            cases = read_cases(resolve_path(args.file or settings["evaluation"]["routing_file"]), "routing")
            print(f"라우팅 형식 검증 통과: {len(cases)}문항")
            return 0
        with dependencies(settings) as deps:
            cases = read_cases(resolve_path(args.file or settings["evaluation"]["routing_file"]), "routing")
            rows, summary = evaluate(cases, lambda case: predict(case, deps))
        path = write_result(args.name, "routing", settings, rows, summary, markdown(rows, summary),
                            dataset_hash=digest(cases))
        print(f"라우팅 정확도 {summary['correct']}/{summary['count']}: {path}")
        return 0

    return guarded(run)


if __name__ == "__main__":
    raise SystemExit(main())
