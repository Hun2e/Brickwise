"""실행 중인 앱·색인을 정지한 뒤 Chroma 스냅샷을 생성한다."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rag_service.eval.common import configured, guarded, parser
from rag_service.eval.snapshot import create_snapshot


def main(argv: list[str] | None = None) -> int:
    cli = parser(__doc__)
    cli.add_argument("--name", required=True)
    args = cli.parse_args(argv)

    def run() -> int:
        manifest = create_snapshot(configured(args), args.name)
        for name, info in manifest["collections"].items():
            print(f"{name}: {info['chunk_count']} chunks / {info['document_count']} documents")
        return 0

    return guarded(run)


if __name__ == "__main__":
    raise SystemExit(main())
