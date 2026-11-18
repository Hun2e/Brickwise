"""본문의 원래 근거 번호를 유지하는 인용 필터."""

import re

from rag_service.rag.evidence import Card

_CITATIONS = re.compile(r"\[근거\s*([\d,\s]+)\]")
NO_CITATION_MESSAGE = "답변에 인용된 근거가 없습니다"


def parse_citations(answer: str) -> list[int]:
    """복합 인용을 포함해 중복 없는 번호를 오름차순으로 반환한다."""
    return sorted({int(number) for group in _CITATIONS.findall(answer)
                   for number in re.findall(r"\d+", group)})


def filter_cited(answer: str, cards: list[Card]) -> tuple[list[Card], bool, list[str]]:
    """유효한 인용만 표시하고 존재하지 않는 번호는 경고한다."""
    numbers = set(parse_citations(answer))
    available = {card.n for card in cards}
    used = sorted((card for card in cards if card.n in numbers), key=lambda card: card.n)
    warnings = [f"[근거 {n}]에 해당하는 근거가 없어 목록에서 제외했습니다."
                for n in sorted(numbers - available)]
    return used, not used, warnings
