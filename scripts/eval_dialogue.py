"""다턴 시나리오를 격리 실행하고 공통 JSON·Markdown 결과를 기록한다."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rag_service.config import resolve_path
from rag_service.eval.common import configured, digest, guarded, parser, write_result
from rag_service.eval.dialogue import evaluate, markdown, read_scenarios, working_data
from rag_service.llm import get_chat_model


def main(argv: list[str] | None = None) -> int:
    cli = parser(__doc__)
    cli.add_argument("--file", required=True)
    cli.add_argument("--reps", type=int)
    cli.add_argument("--name", default="dialogue")
    cli.add_argument("--validate-only", action="store_true")
    args = cli.parse_args(argv)

    def run() -> int:
        settings = configured(args)
        reps = args.reps if args.reps is not None else settings["evaluation"]["repetitions"]
        if reps < 1:
            raise ValueError("반복 수는 양수여야 합니다.")
        scenarios = read_scenarios(resolve_path(args.file))
        if args.validate_only:
            print(f"시나리오 형식 검증: {len(scenarios)}개")
            return 0
        get_chat_model(settings)  # 키 누락은 데이터 복사 전에 안내한다. API 호출은 하지 않는다.
        with working_data(settings) as config:
            rows, summary = evaluate(scenarios, config, reps=reps)
        path = write_result(args.name, "dialogue", settings, rows, summary, markdown(rows, summary),
                            dataset_hash=digest(scenarios), repetitions=reps, isolated_data=True)
        print(f"대화 시나리오 {summary['passed']}/{summary['scenario_runs']} 통과: {path}")
        return 0 if summary["failed"] == 0 else 1

    return guarded(run)


if __name__ == "__main__":
    raise SystemExit(main())
