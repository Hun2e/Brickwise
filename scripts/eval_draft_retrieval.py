"""고정 색인의 섹션으로 검수용 검색 질문 초안을 생성한다."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rag_service.config import resolve_path
from rag_service.eval.common import configured, guarded, parser, table, write_jsonl, write_result
from rag_service.eval.draft import generate_drafts, sample_sections, sections
from rag_service.eval.schema import validate_rows
from rag_service.eval.snapshot import collection_records, load_snapshot
from rag_service.llm import get_chat_model


def main(argv: list[str] | None = None) -> int:
    cli = parser(__doc__)
    cli.add_argument("--snapshot")
    cli.add_argument("--n", type=int)
    cli.add_argument("--name", default="retrieval-draft")
    args = cli.parse_args(argv)

    def run() -> int:
        settings = configured(args)
        model = get_chat_model(settings)
        cfg = settings["evaluation"]
        path = resolve_path(cfg["draft_file"])
        if path.exists():
            raise ValueError("초안 파일이 이미 있습니다. 별도 evaluation.draft_file을 지정하세요.")
        client, manifest = load_snapshot(settings, args.snapshot or cfg["snapshot_name"])
        set_ids = client.get_collection(settings["collections"]["items"]).get(include=[])["ids"]
        pool = sections(collection_records(client.get_collection(settings["collections"]["docs"])), set_ids)
        selected, sampling = sample_sections(pool, args.n if args.n is not None else cfg["draft_count"],
                                             cfg["korean_fraction"], cfg["seed"])
        rows = generate_drafts(selected, model=model, settings=settings, snapshot=manifest)
        validate_rows(rows, "retrieval")
        counts = [[lang, kind, sum(row["lang"] == lang and row["kind"] == kind for row in rows)]
                  for lang in ("ko", "en") for kind in ("entity", "series", "set")]
        report = table(["언어", "문서 종류", "초안 수"], counts)
        report += "\n\n스냅샷에 없는 종류는 0개입니다. PM은 분포와 섹션·질문 중복 비율을 검수하세요."
        result = write_result(args.name, "retrieval_draft", settings, rows, sampling, report, snapshot=manifest)
        write_jsonl(path, rows)
        print(f"초안 {len(rows)}개 생성: {path}; 층화 결과: {result}")
        return 0

    return guarded(run)


if __name__ == "__main__":
    raise SystemExit(main())
