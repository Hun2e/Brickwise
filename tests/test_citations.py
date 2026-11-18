"""근거 표기의 파싱과 원래 번호 보존 계약."""

import pytest

from rag_service.rag.citations import filter_cited, parse_citations
from rag_service.rag.evidence import Card, format_cards, text_hash


def card(n: int, text: str = "본문") -> Card:
    return Card(n, "db", "제목", "https://source.invalid/", "license-only", text,
                chunk_id=f"db:{n}", section_path="섹션")


@pytest.mark.parametrize("answer,expected", [
    ("설명 [근거 2] [근거 3, 5] [근거3]", [2, 3, 5]),
    ("[근거 5, 2, 5] [근거\n3]", [2, 3, 5]),
    ("인용 없음", []), ("[근거 , ] [근거 x] [근거 -1]", []),
])
def test_parse(answer, expected):
    assert parse_citations(answer) == expected


def test_filter_keeps_numbers_and_excludes_invalid():
    answer = "사실 [근거 3, 5] [근거3] [근거 99] [근거 0]"
    used, empty, warnings = filter_cited(answer, [card(5), card(1), card(3)])
    assert [row.n for row in used] == [3, 5]
    assert not empty and len(warnings) == 2
    assert "99" in warnings[1] and "0" in warnings[0]
    assert answer == "사실 [근거 3, 5] [근거3] [근거 99] [근거 0]"


@pytest.mark.parametrize("answer", ["확인되지 않습니다", "[근거 99]"])
def test_no_valid_citation(answer):
    used, empty, _ = filter_cited(answer, [card(1)])
    assert used == [] and empty


def test_format_and_normalization():
    assert format_cards([card(3)]) == "[근거 3] (db · 제목 › 섹션)\n본문"
    assert text_hash("Ａ  B\nC") == text_hash("A B C")
    assert text_hash("영어") != text_hash("English")
