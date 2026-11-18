"""라우터와 실제 후처리 규칙까지만 실행한다. 답변 생성은 실행하지 않는다."""

from typing import Callable

from rag_service.eval.common import table
from rag_service.rag.prompts import router_messages
from rag_service.rag.schemas import RouterOutput, RouterOutputV2
from rag_service.services.orchestrator import OrchestratorDeps, OrchestratorResponse, _cached, _rules


def predict(case: dict, deps: OrchestratorDeps) -> dict:
    selected = None
    if case["selected_set"]:
        row = deps.conn.execute("SELECT set_num, name FROM sets WHERE set_num=?", (case["selected_set"],)).fetchone()
        if row is None:
            raise ValueError("라우팅 문항의 선택 세트가 DB에 없습니다.")
        selected = {"set_num": row[0], "name": row[1]}
    cfg = deps.settings["router"]
    version = cfg["prompt_version"]
    messages = router_messages(case["question"], case["has_collection"], version,
                               selected_set=selected, history=case["history"],
                               history_answer_chars=cfg["history_answer_chars"])
    schema = RouterOutputV2 if version in {"router-v2", "router-v3"} else RouterOutput
    response = OrchestratorResponse()
    out = _cached(deps, messages, schema, version, response, "router")
    if out is None:
        return {"predicted_route": None, "error": "structured_output_failed", "usage": response.usage}
    route, source, resolved = _rules(out, case["question"], case["selected_set"],
                                      case["has_collection"], deps, "llm")
    return {"predicted_route": route, "llm_route": out.route, "route_source": source,
            "needs_input": resolved.needs_input, "usage": response.usage}


def evaluate(cases: list[dict], classify: Callable[[dict], dict]) -> tuple[list[dict], dict]:
    rows = [{**case, **classify(case)} for case in cases]
    labels = sorted({row["expected_route"] for row in rows} |
                    {row.get("predicted_route") or "error" for row in rows})
    confusion = {expected: {predicted: 0 for predicted in labels} for expected in labels}
    for row in rows:
        row["correct"] = row["expected_route"] == row.get("predicted_route")
        confusion[row["expected_route"]][row.get("predicted_route") or "error"] += 1
    return rows, {"count": len(rows), "correct": sum(row["correct"] for row in rows),
                  "accuracy": sum(row["correct"] for row in rows) / len(rows) if rows else 0.0,
                  "confusion": confusion, "incorrect_ids": [row["id"] for row in rows if not row["correct"]]}


def markdown(rows: list[dict], summary: dict) -> str:
    matrix = summary["confusion"]
    labels = list(matrix)
    return (f"정확도: {summary['accuracy']:.4f} ({summary['correct']}/{summary['count']})\n\n"
            + table(["정답 / 예측", *labels], [[key, *matrix[key].values()] for key in labels])
            + "\n\n오답 문항\n\n" + table(["ID", "정답", "예측", "질문"],
                [[row["id"], row["expected_route"], row.get("predicted_route") or "error", row["question"]]
                 for row in rows if not row["correct"]]))
