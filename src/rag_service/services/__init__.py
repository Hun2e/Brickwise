"""UI와 CLI가 공유하는 서비스 인터페이스."""

from rag_service.config import load_settings

from .search import SearchResult, search_sets

__all__ = ["SearchResult", "search_sets", "server_settings"]


def server_settings() -> dict:
    """서버 운영 설정도 공통 설정 로더를 통해 제공한다."""
    return load_settings()["server"]


# 지연 내보내기로 orchestrator ↔ 서비스 모듈 초기화 순환을 피한다.
_CHAT_EXPORTS = {"chat_turn_v2", "create_conversation", "list_conversations", "get_conversation",
                 "change_subject", "delete_conversation", "set_history"}
__all__ += sorted(_CHAT_EXPORTS)


def __getattr__(name: str):
    if name in _CHAT_EXPORTS:
        from . import chat
        return getattr(chat, name)
    raise AttributeError(name)
