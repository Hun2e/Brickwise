"""수집·오케스트레이터 이벤트를 두 UI 공용 단계로 변환한다."""

STAGES = ["분류", "검색어 추출", "문서 수집", "검색", "생성", "검증"]


class StageTracker:
    """질문 한 턴에서 제목 없는 단계는 한 번만 표시하고 상세는 보존한다."""

    def __init__(self) -> None:
        self._seen: set[str] = set()

    def update(self, name: str, data: dict) -> tuple[str, str | None] | None:
        stage = to_stage(name, data)
        if stage is None:
            return None
        label, detail = stage
        if not detail:
            if label in self._seen:
                return None
            self._seen.add(label)
        return stage


def to_stage(name: str, data: dict) -> tuple[str, str | None] | None:
    if name in ("routing", "resolving"):
        return "분류", None
    if name in ("extracting_queries", "extracting"):
        return "검색어 추출", None
    if name == "searching":
        return ("문서 수집" if "q" in data else "검색"), None
    if name == "selected":
        return "문서 수집", f"선택 {len(data['selected'])} · 제외 {data['rejected_count']}"
    if name in ("selecting", "fetching", "indexing", "skipped"):
        detail = data.get("title") if name == "fetching" else None
        if name == "skipped" and data.get("reason") == "cached":
            detail = "이미 수집됨"
        return "문서 수집", detail
    if name in ("querying", "retrieving", "ranking", "reviewing"):
        return "검색", None
    if name in ("generating", "verifying"):
        return ("생성" if name == "generating" else "검증"), None
    return None
