"""GMS 모델 팩토리. import 시에는 비밀 파일을 읽지 않는다."""

import os
from functools import lru_cache

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI, OpenAIEmbeddings

from rag_service.config import load_settings, project_root


class MissingApiKey(RuntimeError):
    """GMS 환경변수가 없는 경우."""


@lru_cache(maxsize=1)
def _load_environment() -> None:
    """첫 모델 생성 시 한 번만 로드하며 기존 환경변수를 우선한다."""
    load_dotenv(project_root() / ".env", override=False)


def _options(settings: dict | None) -> tuple[dict, dict]:
    _load_environment()
    key = os.environ.get("GMS_KEY", "").strip()
    if not key:
        raise MissingApiKey("GMS_KEY가 설정되지 않았습니다.")
    llm = (settings if settings is not None else load_settings())["llm"]
    options = {
        "api_key": key,
        "base_url": llm["base_url"],
        "timeout": llm["timeout_sec"],
        # 래퍼가 재시도 횟수와 사용량을 관측하도록 SDK 재시도는 끈다.
        "max_retries": 0,
    }
    return llm, options


def get_chat_model(settings: dict | None = None) -> ChatOpenAI:
    """설정과 환경변수로 채팅 모델을 만든다 (API 호출 없음)."""
    llm, options = _options(settings)
    if llm["send_temperature"]:
        options["temperature"] = llm["temperature"]
    return ChatOpenAI(model=llm["chat_model"], **options)


def get_embeddings(
    settings: dict | None = None,
    *,
    chunk_size: int | None = None,
    check_embedding_ctx_length: bool | None = None,
) -> OpenAIEmbeddings:
    """동일한 GMS 설정으로 임베딩 클라이언트를 만든다."""
    llm, options = _options(settings)
    if chunk_size is not None:
        options["chunk_size"] = chunk_size
    if check_embedding_ctx_length is not None:
        options["check_embedding_ctx_length"] = check_embedding_ctx_length
    return OpenAIEmbeddings(model=llm["embedding_model"], **options)
