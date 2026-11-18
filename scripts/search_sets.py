"""자연어 조건으로 세트를 탐색하고 선택용 번호·근거·검수 결과를 출력한다."""

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rag_service.config import load_settings, resolve_path
from rag_service.llm import MissingApiKey, get_chat_model, get_embeddings
from rag_service.services import search_sets


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--question", required=True, help="자연어 탐색 조건")
    args = parser.parse_args(argv)

    def event(name: str, data: dict) -> None:
        print(json.dumps({"event": name, **data}, ensure_ascii=False), file=sys.stderr)

    try:
        settings = load_settings()
        model = get_chat_model(settings)
        # 키 누락·도움말 경로에서는 Chroma 초기화나 DB 생성도 하지 않는다.
        from rag_service.vectorstore.embed_cache import CachedEmbeddings
        from rag_service.vectorstore.store import get_collection

        embeddings = CachedEmbeddings(
            get_embeddings(settings), resolve_path(settings["paths"]["embed_cache"]),
            settings["llm"]["embedding_model"], settings["index"]["embed_batch_size"])
        store = get_collection(settings["collections"]["items"], embeddings, settings)
        path = resolve_path(settings["paths"]["sqlite"])
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
            result = search_sets(args.question, conn=conn, model=model, meta_store=store,
                                 settings=settings, on_event=event)
        output = asdict(result)
        output["query"] = result.query.model_dump() if result.query else None
        output["dropped"] = [item.model_dump() for item in result.dropped]
        print(json.dumps(output, ensure_ascii=False))
        return 0
    except MissingApiKey:
        print("GMS_KEY가 설정되지 않았습니다. 환경변수에 키를 설정한 뒤 다시 실행하세요.")
        return 2
    except Exception:
        print("조건 탐색 실패: DB·항목 색인·설정과 연결 상태를 확인하세요.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
