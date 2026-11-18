"""키·네트워크 없이 RAG 그래프의 Mermaid를 출력하고 저장한다."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rag_service.config import resolve_path
from rag_service.rag.graph import RagDeps, build_graph


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="docs/screenshots/rag_graph.mmd", help="Mermaid 저장 경로")
    args = parser.parse_args(argv)
    mermaid = build_graph(RagDeps()).get_graph().draw_mermaid()
    path = resolve_path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(mermaid, encoding="utf-8")
    print(mermaid, end="" if mermaid.endswith("\n") else "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
