"""검색·DB 결과를 같은 형식으로 전달하는 근거 카드."""

import hashlib
import unicodedata
from dataclasses import dataclass
from typing import Literal

SourceType = Literal["set_meta", "db", "wikipedia", "brickipedia"]


@dataclass(frozen=True)
class Card:
    n: int
    source_type: SourceType
    title: str
    url: str
    license: str
    text: str
    item_id: str = ""
    doc_id: str = ""
    chunk_id: str = ""
    score: float | None = None
    lang: str = ""
    entity_kind: str = ""
    section_path: str = ""


def text_hash(text: str) -> str:
    """유니코드·공백 차이만 제거해 서로 다른 언어의 본문은 보존한다."""
    normalized = " ".join(unicodedata.normalize("NFKC", text).split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def format_cards(cards: list[Card]) -> str:
    """URL·라이선스를 제외한 생성용 근거를 만든다."""
    return "\n\n".join(
        f"[근거 {card.n}] ({card.source_type} · {card.title}"
        f"{' › ' + card.section_path if card.section_path else ''})\n{card.text}"
        for card in cards
    )
