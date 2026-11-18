"""TextExtracts 평문을 섹션으로 나누고 참고 자료 영역을 제거한다."""

import re

from .base import Section


_HEADING = re.compile(r"^\s*(={2,})\s*(.*?)\s*={2,}\s*$")


def split_sections(extract: str) -> list[Section]:
    """잘못 중첩된 등호도 제목 가장자리에서 제거한다."""
    sections: list[Section] = []
    heading, level, lines = "", 1, []
    for line in extract.splitlines():
        match = _HEADING.match(line)
        if match and match.group(2).strip("= \t"):
            sections.append(Section(heading, level, "\n".join(lines)))
            heading, level, lines = match.group(2).strip("= \t"), len(match.group(1)), []
        else:
            lines.append(line)
    sections.append(Section(heading, level, "\n".join(lines)))
    return sections


def clean_sections(sections: list[Section], drop_headings: list[str],
                   min_chars: int) -> list[Section]:
    """제외 제목의 하위 트리와 짧거나 빈 섹션을 제거한다."""
    dropped = {heading.strip().casefold() for heading in drop_headings}
    result: list[Section] = []
    drop_level: int | None = None
    for section in sections:
        if drop_level is not None:
            if section.level > drop_level:
                continue
            drop_level = None
        heading = section.heading.strip()
        if heading.casefold() in dropped:
            drop_level = section.level
            continue
        text = "\n".join(re.sub(r"[^\S\n]+", " ", line).strip() for line in section.text.splitlines())
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        if text and len(text) >= min_chars:
            result.append(Section(heading, section.level, text))
    return result
