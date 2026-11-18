"""최신 인벤토리와 DB 공식 수치로 세트당 메타 문서 하나를 만든다."""

import re
import sqlite3
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass

from rag_service.config import load_settings
from .aliases import aliases_for


@dataclass(frozen=True)
class ItemDoc:
    set_num: str
    text: str
    metadata: dict


# 일반 명칭 뒤에 붙는 외형·소속 설명은 캐릭터 이름으로 승격하지 않는다.
_VARIANT = re.compile(
    r"\b(black|white|gr[ae]y|brown|blue|red|green|yellow|orange|silver|gold|"
    r"tan|nougat|azure|turquoise|pink|purple|lavender|dark|light|medium|pearl|"
    r"phase|armor|armour|arm|arms|head|helmet|body|print|printed|markings|"
    r"legion|battalion|company|corps|gunner|airborne|artillery|imperial|"
    r"kashyyyk|geonosis|coruscant|first order|new republic)\b", re.IGNORECASE)


def short_minifig_name(name: str, generic_prefixes: list[str] | None = None) -> str:
    """쉼표·공백 하이픈과 with 설명을 떼고 고유 이름을 보존한다."""
    if generic_prefixes is None:
        generic_prefixes = load_settings()["index"]["generic_minifig_prefixes"]
    pieces = [piece.partition(" with ")[0].strip() for piece in re.split(r",| - ", name)]
    first = pieces[0]
    if (first.casefold() in {prefix.casefold() for prefix in generic_prefixes}
            and len(pieces) > 1 and pieces[1] and not _VARIANT.search(pieces[1])):
        return pieces[1]
    return first


def _build_item_doc(conn: sqlite3.Connection, set_num: str, prefixes: list[str]) -> ItemDoc:
    row = conn.execute("""SELECT s.name, s.year, s.num_parts,
        COALESCE(rt.root_theme_name, t.name), sc.scope FROM sets s
        JOIN set_scope sc ON sc.set_num=s.set_num
        LEFT JOIN set_root_theme rt ON rt.set_num=s.set_num
        LEFT JOIN themes t ON t.id=s.theme_id WHERE s.set_num=?""", (set_num,)).fetchone()
    if row is None:
        raise ValueError("범위에 없는 세트입니다. 데이터 적재와 세트 번호를 확인하세요.")
    name, year, num_parts, theme, scope = row
    entities = [r[0] for r in conn.execute("""SELECT wiki_title FROM set_entities
        WHERE set_num=? ORDER BY rule_index, wiki_title""", (set_num,))]
    verified, unverified = aliases_for(conn, entities)
    figs: Counter[str] = Counter()
    for fig_name, quantity in conn.execute("""SELECT m.name, im.quantity
        FROM inventory_minifigs im JOIN minifigs m ON m.fig_num=im.fig_num
        WHERE im.inventory_id=(SELECT id FROM inventories WHERE set_num=?
            ORDER BY version DESC, id DESC LIMIT 1)""", (set_num,)):
        figs[short_minifig_name(fig_name, prefixes)] += quantity
    count = sum(figs.values())
    lines = [f"세트: {name} ({set_num})"]
    if verified:
        lines.append("별칭: " + ", ".join(verified))
    if unverified:
        lines.append("검색어: " + ", ".join(unverified))
    lines.extend([f"연도: {year} · 테마: {theme} · 공식 부품 수: {num_parts:,}개",
                  f"미니피그({count}): " + (", ".join(sorted(figs)) or "없음")])
    if entities:
        lines.append("관련: " + ", ".join(entities))
    metadata = {"source_type": "set_meta", "set_num": set_num, "name": name,
                "year": year, "theme": theme, "num_parts": num_parts,
                "num_minifigs": count, "scope": scope, "has_ko_alias": bool(verified)}
    if entities:
        metadata["entities"] = entities
    return ItemDoc(set_num, "\n".join(lines), metadata)


def build_item_doc(conn: sqlite3.Connection, set_num: str) -> ItemDoc:
    """세트 하나의 텍스트와 Chroma 메타데이터를 반환한다."""
    return _build_item_doc(conn, set_num, load_settings()["index"]["generic_minifig_prefixes"])


def iter_item_docs(conn: sqlite3.Connection, scopes: list[str]) -> Iterator[ItemDoc]:
    """설정된 범위의 세트를 번호순으로 한 번씩 생성한다."""
    prefixes = load_settings()["index"]["generic_minifig_prefixes"]
    marks = ",".join("?" for _ in scopes)
    numbers = conn.execute(f"SELECT set_num FROM set_scope WHERE scope IN ({marks}) ORDER BY set_num", scopes)
    for (number,) in numbers:
        yield _build_item_doc(conn, number, prefixes)
