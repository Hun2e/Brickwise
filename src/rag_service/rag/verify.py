"""인용된 카드·질문과 DB만으로 수치를 결정적으로 검증한다."""

import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from rag_service.config import load_settings
from rag_service.rag.citations import filter_cited
from rag_service.rag.evidence import Card

UNCONFIRMED = "제공된 자료에서 확인되지 않습니다"
_CITATION = r"\[근거\s*[\d,\s]+\]"
_CITATIONS = re.compile(_CITATION)
_PREFIX = re.compile(r"(?m)^(\s*(?:#{1,6}\s+)?)(?:\d+[.)]\s+)")
_HEADING = re.compile(r"(?m)^(\s*#{1,6}\s+)\d+\s+")
_NUMBER = re.compile(r"(?<![\dA-Za-z])(?:\d{4,7}-\d{1,2}(?!\d)|[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)(?!\d)")
_SETNUM = re.compile(r"\d{4,7}-\d{1,2}\Z")
_UNIT = re.compile(
    r"\s*(?:[*_`]+\s*)*(?:%|퍼센트|개|명|종|년|월|일|회|대|배|번|단계|시간|분|초|원|달러|파섹|미터|킬로|톤|"
    r"(?:pieces?|parts?|people|years?|months?|days?|times?|percent|parsecs?|km|cm|mm|kg|m|g|s)\b)", re.I)
_SMALL = dict(zip("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty".split(), range(21)))
_TENS = dict(zip("thirty forty fifty sixty seventy eighty ninety".split(), range(30, 100, 10)))
_WORDS = {**_SMALL, **_TENS, "hundred": 100, "thousand": 1000}
_ENGLISH = re.compile(r"\b(?:" + "|".join(_WORDS) + r")(?:[ -]+(?:and[ -]+)?(?:" + "|".join(_WORDS) + r"))*\b", re.I)


@dataclass
class VerifyResult:
    answer: str
    removed: list[dict]
    checked: int
    warnings: list[str]
    items: list[dict] = field(default_factory=list)


def split_sentences(answer: str) -> list[str]:
    """소수·목록 번호와 문장 끝 뒤 인용을 보존하는 문장/행 분리."""
    result = []
    for line in answer.splitlines(keepends=True):
        if result and re.fullmatch(r"\s*(?:" + _CITATION + r"\s*)+", line):
            result[-1] += line
            continue
        prefix = _PREFIX.match(line)
        start = 0
        for match in re.finditer(r"[.!?。！？]+(?:[\"'”’)]*)(?:[ \t]*" + _CITATION + r")*(?=\s|$)", line):
            if prefix and match.start() < prefix.end():
                continue
            end = match.end()
            result.append(line[start:end])
            start = end
        if line[start:]:
            if not line[start:].strip() and result:
                result[-1] += line[start:]
            else:
                result.append(line[start:])
    return [sentence for sentence in result if sentence.strip()]


def _normalize(value: str) -> str:
    if _SETNUM.fullmatch(value):
        return value
    number = Decimal(value.replace(",", ""))
    if not number:
        return "0"
    return format(number, "f").rstrip("0").rstrip(".") if "." in format(number, "f") else str(number)


def _number_tokens(text: str, ignore_single_digit: bool = False) -> list[tuple[str, bool]]:
    clean = _HEADING.sub(r"\1", _PREFIX.sub(r"\1", _CITATIONS.sub("", text)))
    values = []
    for match in _NUMBER.finditer(clean):
        raw = match.group()
        if ignore_single_digit and re.fullmatch(r"\d", raw) and not _UNIT.match(clean, match.end()):
            continue
        is_set = bool(_SETNUM.fullmatch(raw) or (re.fullmatch(r"\d{5}", raw)
                                                and not _UNIT.match(clean, match.end())))
        values.append((raw if is_set else _normalize(raw), is_set))
    return values


def _numbers(text: str) -> list[str]:
    return [value for value, _ in _number_tokens(text)]


def _english_values(text: str) -> set[str]:
    values = set()
    for match in _ENGLISH.finditer(text):
        total = current = 0
        for word in re.split(r"[ -]+", match.group().lower()):
            if word == "and":
                continue
            if word == "hundred":
                current = (current or 1) * 100
            elif word == "thousand":
                total += (current or 1) * 1000
                current = 0
            else:
                current += _WORDS[word]
        values.add(str(total + current))
    return values


def allowed_values(cards: list[Card], question: str) -> set[str]:
    """본문만 사용한다. URL·제목·메타데이터는 수치 근거가 아니다."""
    values = set(_numbers(question))
    for card in cards:
        values.update(_numbers(card.text))
        values.update(_english_values(card.text))
    return _set_aliases(values)


def _set_aliases(values: set[str]) -> set[str]:
    values = set(values)
    # 접미사를 생략해도 되지만 명시된 다른 변형까지 허용하지는 않는다.
    values.update(value + "-1" for value in tuple(values) if re.fullmatch(r"\d{5}", value))
    values.update(value.split("-")[0] for value in tuple(values) if _SETNUM.fullmatch(value))
    return values


def _set_exists(value: str, conn: sqlite3.Connection) -> bool:
    if _SETNUM.fullmatch(value):
        return conn.execute("SELECT 1 FROM sets WHERE set_num=?", (value,)).fetchone() is not None
    return conn.execute("SELECT 1 FROM sets WHERE set_num=? OR set_num GLOB ?",
                        (value, value + "-[0-9]*")).fetchone() is not None


def verify_answer(answer: str, cited_cards: list[Card], question: str,
                  conn: sqlite3.Connection, context_set_nums: Sequence[str] = (),
                  *, settings: dict | None = None) -> VerifyResult:
    """위반 문장을 제거하고 원본의 0 기반 문장 위치를 기록한다."""
    cfg = (settings if settings is not None else load_settings())["verify"]
    if not cfg["enabled"]:
        return VerifyResult(answer, [], 0, [])
    cards, _, _ = filter_cited(answer, cited_cards)
    allowed = allowed_values(cards, question)
    # 문맥에서는 세트 식별자만 허용한다. 접미사를 일반 수량 근거로 사용하지 않는다.
    context_numbers = {number.strip() for number in context_set_nums
                       if _SETNUM.fullmatch(number.strip()) or re.fullmatch(r"\d{5}", number.strip())}
    allowed.update(_set_aliases({number for number in context_numbers if _set_exists(number, conn)}))
    kept, removed, items = [], [], []
    checked = 0
    exists: dict[str, bool] = {}
    for index, sentence in enumerate(split_sentences(answer)):
        invalid = []
        for value, is_set in _number_tokens(sentence, cfg["ignore_single_digit"]):
            checked += 1
            if is_set and value not in exists:
                exists[value] = _set_exists(value, conn)
            if value not in allowed or (is_set and not exists[value]):
                if value not in invalid:
                    invalid.append(value)
                    items.append({"value": value, "sentence_index": index})
        if invalid:
            removed.append({"sentence": sentence.strip(), "values": invalid})
        else:
            kept.append(sentence)
    cleaned = "".join(kept).strip() if removed else answer
    if removed and (not cleaned or not filter_cited(cleaned, cards)[0]):
        cleaned = UNCONFIRMED
    warnings = [f"검증되지 않은 수치를 포함한 문장 {len(removed)}개를 제외했습니다"] if removed else []
    return VerifyResult(cleaned, removed, checked, warnings, items)
