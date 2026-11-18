"""운영 기본값과 독립적으로 이전 검색 동작을 검증하기 위한 설정."""


def configure_similarity(settings: dict) -> None:
    """similarity·확장 질의·상한을 사용하는 테스트 조건을 명시한다."""
    settings["retrieval"].update(
        search_type="similarity", query_mode="expanded",
        mmr_apply_caps=True, similarity_fetch_k=20,
    )
