"""PM 검수 문항과 고정 스냅샷으로 검색 품질을 채점한다."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rag_service.config import resolve_path
from rag_service.eval.common import configured, digest, guarded, parser, write_result
from rag_service.eval.experiments import (MissingQueryEmbedding, contexts, grid_markdown, measure,
                                         read_grid, retrieval_stores, run_grid)
from rag_service.eval.retrieval import compare_results, markdown
from rag_service.eval.schema import read_cases
from rag_service.eval.snapshot import temporary_snapshot


def main(argv: list[str] | None = None) -> int:
    cli = parser(__doc__)
    cli.add_argument("--snapshot")
    cli.add_argument("--file")
    cli.add_argument("--name", help="결과 이름 또는 grid 파일명의 접두사")
    cli.add_argument("--baseline", help="같은 문항·스냅샷의 이전 결과 JSON")
    cli.add_argument("--grid", help="단계별 E1~E4 실험 YAML")
    cli.add_argument("--embed-missing", action="store_true", help="누락된 질문 벡터만 API 호출 후 캐시")
    args = cli.parse_args(argv)

    def run() -> int:
        settings = configured(args)
        cfg = settings["evaluation"]
        cases = read_cases(resolve_path(args.file or cfg["retrieval_file"]), "retrieval")
        with temporary_snapshot(settings, args.snapshot or cfg["snapshot_name"]) as (client, manifest):
            meta_store, docs_store = retrieval_stores(client, settings, args.embed_missing)
            prepared, used_terms = contexts(cases, meta_store, settings)
            baseline = None
            if args.baseline:
                baseline = json.loads(resolve_path(args.baseline).read_text(encoding="utf-8"))
                if baseline.get("en_terms") != used_terms:
                    raise ValueError("기준선과 P1 확장어가 다릅니다.")

            def experiment(name: str, config: dict) -> dict:
                rows, summary = measure(cases, docs_store, config, prepared)
                result = dict(name=name, settings=config, rows=rows, summary=summary)
                if baseline is not None:
                    result["comparison"] = compare_results(rows, baseline, manifest, config)
                    result["baseline_ranked_equal"] = ([row["ranked"] for row in rows] ==
                                                       [row["ranked"] for row in baseline["rows"]])
                return result

            metadata = dict(snapshot=manifest, dataset_hash=digest(cases), en_terms=used_terms,
                            collect_fn=None, llm_calls=0)
            if args.grid:
                grid = read_grid(args.grid)
                results, selections = run_grid(settings, grid, experiment)
                reference = {**results[0], "snapshot": manifest}
                prefix = args.name or grid["name"]
                for result in results:
                    if "comparison" not in result and not result.get("blocked_reason"):
                        result["comparison"] = compare_results(result["rows"], reference, manifest, result["settings"])
                    report = ("미측정: " + result["blocked_reason"] if result.get("blocked_reason") else
                              markdown(result["summary"], result["comparison"]))
                    write_result(f"{prefix}-{result['name']}", "retrieval", result["settings"],
                                 result["rows"], result["summary"], report,
                                 **metadata, comparison=result.get("comparison"), decision=result["decision"],
                                 blocked_reason=result.get("blocked_reason"),
                                 baseline_ranked_equal=result.get("baseline_ranked_equal"), parent=result.get("parent"))
                blocked = [row["name"] for row in results if row.get("blocked_reason")]
                path = write_result(f"{prefix}-grid", "retrieval-grid", settings, results,
                                    {"selections": selections, "combinations": len(results), "blocked": blocked},
                                    grid_markdown(results, selections), **metadata, grid=grid)
                if blocked:
                    print(f"질의 벡터 캐시 누락으로 {len(blocked)}개 조합 미측정. 부분 결과: {path}", file=sys.stderr)
                    return 1
            else:
                name = args.name or "baseline"
                result = experiment(name, settings)
                path = write_result(name, "retrieval", settings, result["rows"], result["summary"],
                                    markdown(result["summary"], result.get("comparison")), **metadata,
                                    comparison=result.get("comparison"),
                                    baseline_ranked_equal=result.get("baseline_ranked_equal"))
            print(f"검색 채점 {len(cases)}문항: {path}")
            return 0

    def checked() -> int:
        try:
            return run()
        except MissingQueryEmbedding as error:
            print(str(error), file=sys.stderr)
            return 1

    return guarded(checked)


if __name__ == "__main__":
    raise SystemExit(main())
