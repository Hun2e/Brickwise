"""출처에 독립적인 검색 후보와 수집 문서."""

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class Candidate:
    source: str
    lang: str
    page_id: int
    title: str
    snippet: str
    url: str


@dataclass(frozen=True)
class Section:
    heading: str
    level: int
    text: str


@dataclass
class Doc:
    doc_id: str
    source: str
    lang: str
    page_id: int
    title: str
    url: str
    revid: int
    license: str
    attribution: str
    sections: list[Section]
    fetched_at: str
    search_query: str | None
    cleaning_stats: dict[str, int] = field(default_factory=dict)

    def text(self) -> str:
        """첫머리와 제목 있는 섹션을 평문으로 연결한다."""
        return "\n\n".join(
            f"## {section.heading}\n{section.text}" if section.heading else section.text
            for section in self.sections
        )


class Collector(Protocol):
    source: str

    def search(self, q: str, lang: str, limit: int) -> list[Candidate]: ...

    def latest_revid(self, lang: str, title_or_id: str | int) -> tuple[int, int, str] | None: ...

    def fetch(self, lang: str, title_or_id: str | int,
              search_query: str | None = None) -> Doc | None: ...
