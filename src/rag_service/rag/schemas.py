"""Router와 검색어 추출의 공유 출력 계약."""

from typing import Literal

from pydantic import BaseModel, Field

Route = Literal["fact", "filter", "set_ops", "narrative", "compare", "build", "build_archive"]


class RouterOutput(BaseModel):
    """질문의 의도와 원문에서 추출한 엔티티."""

    route: Route
    set_nums: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)


class RouterOutputV2(RouterOutput):
    """선택 세트 지시어를 구분하는 v2 계약. v1 스키마는 보존한다."""

    set_refs: list[Literal["selected"]] = Field(default_factory=list)


class SelectionCriteria(BaseModel):
    """추천 후보 중 코드가 적용할 정렬·부족 상한."""

    sort: Literal["year", "missing", "coverage", "num_parts"] = "missing"
    order: Literal["asc", "desc"] = "asc"
    max_missing: int | None = Field(default=None, ge=0, strict=True)


class SearchQuery(BaseModel):
    """외부 문서 검색어 하나."""

    q: str
    lang: Literal["en", "ko"]
    purpose: str


class QueryExtraction(BaseModel):
    """T-004/P1 v1 호환 계약: 검색어 4개, 목적 길이 제한 없음."""

    queries: list[SearchQuery] = Field(max_length=4)
    en_terms: list[str] = Field(default_factory=list)


class SearchQueryV2(SearchQuery):
    """P1 v2의 짧은 검색 목적."""

    purpose: str = Field(max_length=40)


class QueryExtractionV2(QueryExtraction):
    """P1 v2의 검색어는 최대 3개다."""

    queries: list[SearchQueryV2] = Field(max_length=3)


def p1_schema(version: str) -> type[QueryExtraction]:
    """프롬프트와 응답 검증에 같은 버전 계약을 적용한다."""
    return {"p1-v1": QueryExtraction, "p1-v2": QueryExtractionV2,
            "p1-v2.1": QueryExtractionV2}[version]


class Selected(BaseModel):
    """후보 ID와 짧은 선정 이유."""

    id: str
    reason: str = Field(max_length=40)


class CandidateSelection(BaseModel):
    """P2의 후보 선택 결과. 실제 선택 상한은 수집 설정으로 제한한다."""

    selected: list[Selected] = Field(max_length=5)


class FilterQuery(BaseModel):
    """자연어 조건 탐색의 추출 계약."""

    year_from: int | None = None
    year_to: int | None = None
    parts_min: int | None = None
    parts_max: int | None = None
    subtheme: Literal["Ultimate Collector Series"] | None = None
    minifig: str | None = None
    has_minifig: bool | None = None
    name_keywords: list[str] = Field(default_factory=list, max_length=3)
    exclude_keywords: list[str] = Field(default_factory=list, max_length=5)
    semantic: str | None = None
    sort: Literal["relevance", "year", "num_parts", "name"] = "relevance"
    order: Literal["asc", "desc"] = "asc"
    limit: int | None = Field(default=None, ge=1, le=50)


class Dropped(BaseModel):
    """의미 조건 검수에서 제외한 후보와 짧은 사유."""

    id: str
    reason: str = Field(max_length=20)


class ReviewResult(BaseModel):
    """검수 결과의 ID는 호출자가 SQL 후보와 대조한다."""

    keep: list[str]
    drop: list[Dropped] = Field(default_factory=list)
