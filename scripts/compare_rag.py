"""확정 문항으로 단독 모델(A)과 운영 서비스(B)를 각 한 번 비교한다."""

import argparse
import sys
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def main(argv: list[str] | None = None) -> int:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--file", required=True, help="PM 확정 비교 문항 JSONL")
    cli.add_argument("--name", required=True, help="같은 날짜에 중복되지 않는 결과 이름")
    args = cli.parse_args(argv)

    from rag_service.config import load_settings, resolve_path
    from rag_service.eval.common import digest
    from rag_service.eval.rag_compare import check_facts, report_paths, run_case, write_reports
    from rag_service.eval.runtime import dependencies
    from rag_service.eval.schema import read_cases
    from rag_service.llm import MissingApiKey, get_chat_model
    from rag_service.services.orchestrator import answer, log_exception

    settings = None
    try:
        settings = deepcopy(load_settings())
        settings["llm"]["cache_enabled"] = False
        if not settings["llm"]["send_temperature"] or settings["llm"]["temperature"] != 0:
            raise ValueError("비교 설정은 temperature 0을 전송해야 합니다.")
        cases = read_cases(resolve_path(args.file), "rag_compare")
        for case in cases:
            check_facts("", case["facts"])
        started = datetime.now(timezone.utc)
        report_paths(settings, args.name, started)
        solo_model = get_chat_model(settings)
        solo_model.cache = False
        with ExitStack() as stack:
            deps = None

            def service(question: str, *, selected_set: str):
                nonlocal deps
                # A의 첫 호출 전에는 DB·색인·수집 자원도 열지 않는다.
                if deps is None:
                    deps = stack.enter_context(dependencies(settings, live=True))
                    deps.model.cache = False
                return answer(question, selected_set=selected_set, deps=deps)

            rows = [run_case(case, solo_model=solo_model, answer_fn=service, settings=settings) for case in cases]
        paths = write_reports(rows, settings=settings, name=args.name, started_at=started,
                              dataset_hash=digest(cases))
        failures = sum(bool(row["B"]["needs_input"]) for row in rows)
        print(f"RAG 비교 {len(rows)}문항 (B 추가 입력/실패 {failures}건): {paths[0]}")
        return 1 if failures else 0
    except MissingApiKey:
        print("API 키가 설정되지 않았습니다. 실행 환경에 GMS_KEY를 설정하세요.", file=sys.stderr)
    except Exception as exc:
        log_exception(exc, settings)
        print("RAG 비교에 실패했습니다. 입력 파일·중복 이름·DB·색인·설정과 연결 상태를 확인하세요.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
