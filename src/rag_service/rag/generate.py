"""P3 자유 텍스트 생성과 실제 인용 근거 필터링."""

from time import perf_counter
from typing import TYPE_CHECKING, Callable

from rag_service.llm import MissingApiKey
from rag_service.rag.citations import filter_cited
from rag_service.rag.prompts import p3_messages

if TYPE_CHECKING:
    from rag_service.rag.graph import RagDeps, RagState


def generate(state: "RagState", deps: "RagDeps",
             on_event: Callable[[str, dict], None] | None = None) -> dict:
    """근거가 없으면 모델을 호출하지 않고 확인 불가를 안내한다."""
    started = perf_counter()
    if on_event:
        on_event("generating", {})
    cards = state.get("cards", [])
    warnings = list(state.get("warnings", []))
    answer = "제공된 자료에서 확인되지 않습니다"
    usage = {"calls": 0}
    if cards:
        cfg = deps.settings["retrieval"]
        messages = p3_messages(state["question"], cards, state.get("history"),
                               history_turns=cfg["history_turns"], version=cfg["p3_prompt_version"])
        for _ in range(deps.settings["llm"]["max_retries"] + 1):
            usage["calls"] += 1
            try:
                raw = deps.model.invoke(messages)
                tokens = getattr(raw, "usage_metadata", None) or (
                    getattr(raw, "response_metadata", {}) or {}).get("token_usage", {})
                for key, legacy in (("input_tokens", "prompt_tokens"),
                                    ("output_tokens", "completion_tokens")):
                    value = tokens.get(key, tokens.get(legacy))
                    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                        usage[key] = usage.get(key, 0) + value
                content = raw if isinstance(raw, str) else raw.content
                if isinstance(content, list):
                    content = "\n".join(block if isinstance(block, str) else block.get("text", "")
                                        for block in content if isinstance(block, (str, dict)))
                if not isinstance(content, str) or not content.strip():
                    raise ValueError("Empty answer")
                answer = content.strip()
                break
            except MissingApiKey:
                raise
            except Exception:
                continue
        else:
            answer = "답변 생성에 실패했습니다. 연결 상태를 확인한 뒤 다시 시도하세요."
            warnings.append(answer)
    used, no_citation, citation_warnings = filter_cited(answer, cards)
    result = {"answer": answer, "used_cards": used, "no_citation": no_citation,
              "warnings": warnings + citation_warnings,
              "usage": {**state.get("usage", {}), "p3": usage},
              "timings": {**state.get("timings", {}),
                          "generate_ms": round((perf_counter() - started) * 1000)}}
    if on_event:
        on_event("done", {"no_citation": no_citation})
    return result
