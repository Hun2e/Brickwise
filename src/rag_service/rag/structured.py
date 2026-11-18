"""구조화 호출, 제한된 재시도, 원문 JSON 복구와 안전한 측정값."""

import json
from dataclasses import dataclass
from time import perf_counter
from typing import Any, Generic, TypeVar

from langchain_core.messages import BaseMessage
from pydantic import BaseModel

from rag_service.llm import MissingApiKey

T = TypeVar("T", bound=BaseModel)
STRUCTURED_METHODS = ("json_schema", "function_calling", "json_mode")


@dataclass
class StructuredResult(Generic[T]):
    value: T | None
    ok: bool
    method: str
    attempts: int
    latency_ms: int
    usage: dict
    error_type: str | None
    temperature_error: bool = False


def _add_usage(total: dict, raw: Any) -> None:
    usage = getattr(raw, "usage_metadata", None)
    if not usage:
        usage = (getattr(raw, "response_metadata", None) or {}).get("token_usage", {})
    for key, legacy in (("input_tokens", "prompt_tokens"), ("output_tokens", "completion_tokens")):
        value = usage.get(key, usage.get(legacy))
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            total[key] = total.get(key, 0) + value


def _raw_text(raw: Any) -> str:
    content = getattr(raw, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            block if isinstance(block, str) else block.get("text", "")
            for block in content if isinstance(block, (str, dict))
        )
    return ""


def _first_object(text: str) -> dict:
    decoder = json.JSONDecoder()
    for index, character in enumerate(text):
        if character == "{":
            try:
                value, _ = decoder.raw_decode(text, index)
                return value
            except json.JSONDecodeError:
                continue
    raise ValueError("JSON object not found")


def invoke_structured(
    model: Any, messages: list[BaseMessage], schema: type[T], method: str, max_retries: int
) -> StructuredResult[T]:
    """재시도 후 받은 원문만 복구한다. 대체 경로는 추가 API 호출을 하지 않는다."""
    started = perf_counter()
    usage: dict[str, int] = {}
    attempts = 0
    raw_messages: list[Any] = []
    error_type: str | None = None
    temperature_error = False

    def result(value: T | None, actual_method: str) -> StructuredResult[T]:
        return StructuredResult(
            value, value is not None, actual_method, attempts,
            round((perf_counter() - started) * 1000), usage,
            None if value is not None else error_type, temperature_error,
        )

    def record_error(exc: Exception) -> None:
        nonlocal error_type, temperature_error
        error_type = type(exc).__name__
        # 메시지 자체는 보존하지 않고 실험에 필요한 여부만 기록한다.
        temperature_error |= "temperature" in str(exc).lower()

    try:
        if method not in STRUCTURED_METHODS or max_retries < 0:
            raise ValueError("Invalid structured call options")
        runner = model.with_structured_output(schema, method=method, include_raw=True)
    except MissingApiKey:
        raise
    except Exception as exc:
        record_error(exc)
        return result(None, method)

    for _ in range(max_retries + 1):
        attempts += 1
        try:
            response = runner.invoke(messages)
            raw = response.get("raw")
            if raw is not None:
                raw_messages.append(raw)
                _add_usage(usage, raw)
            if response.get("parsing_error") is not None:
                raise response["parsing_error"]
            parsed = response.get("parsed")
            if isinstance(parsed, BaseModel):
                parsed = parsed.model_dump()
            value = schema.model_validate(parsed)
            return result(value, method)
        except MissingApiKey:
            raise
        except Exception as exc:
            record_error(exc)

    for raw in reversed(raw_messages):
        try:
            value = schema.model_validate(_first_object(_raw_text(raw)))
            return result(value, "text_extract")
        except Exception:
            # 최종 구조화 실패 원인을 남긴다 (단순 JSON 탐색 실패로 덮어쓰지 않음).
            continue
    return result(None, method)
