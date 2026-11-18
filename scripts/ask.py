"""선택 세트 질문·대화 이력을 받아 답변과 실제 인용 근거를 출력한다."""

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rag_service.llm import MissingApiKey
from rag_service.rag.citations import NO_CITATION_MESSAGE
from rag_service.rag.graph import ask


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--set-num", required=True, help="선택 세트 번호")
    parser.add_argument("--question", required=True, help="질문")
    parser.add_argument("--history", default="[]", help="question/answer 턴 또는 role/content 메시지의 JSON 배열")
    args = parser.parse_args(argv)
    try:
        history = json.loads(args.history)
        if not isinstance(history, list) or any(not isinstance(row, dict) for row in history):
            raise ValueError
    except (ValueError, TypeError):
        print("대화 이력은 JSON 객체 배열로 입력하세요.", file=sys.stderr)
        return 2

    def event(name: str, data: dict) -> None:
        print(json.dumps({"event": name, **data}, ensure_ascii=False), file=sys.stderr)

    try:
        result = ask(args.set_num, args.question, history=history, on_event=event)
        output = {key: result[key] for key in ("answer", "no_citation", "warnings", "usage", "timings")}
        output["used_cards"] = [asdict(card) for card in result["used_cards"]]
        if result["no_citation"]:
            output["notice"] = NO_CITATION_MESSAGE
        print(json.dumps(output, ensure_ascii=False))
        return 0
    except MissingApiKey:
        print("GMS_KEY가 설정되지 않았습니다. 환경변수에 키를 설정한 뒤 다시 실행하세요.")
        return 2
    except Exception:
        print("질문 처리 실패: 세트 번호·DB·색인·설정과 연결 상태를 확인하세요.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
