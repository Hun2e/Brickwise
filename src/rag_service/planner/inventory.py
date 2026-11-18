"""미리 집계된 세트 부품과 사용자 보유량의 계산."""

import sqlite3
from collections import Counter
from collections.abc import Sequence

from .sets import normalize_set_num

Key = tuple[str, int] | str


def set_parts(conn: sqlite3.Connection, set_num: str) -> Counter[tuple[str, int]]:
    """set_parts_flat을 추가 전개 없이 읽는다."""
    number = normalize_set_num(conn, set_num)
    return Counter({(part, color): qty for part, color, qty in conn.execute(
        "SELECT part_num, color_id, quantity FROM set_parts_flat WHERE set_num = ?",
        (number,),
    )})


def rekey(counter: Counter[tuple[str, int]], mode: str) -> Counter[Key]:
    """색 무시 모드에서는 같은 부품의 모든 색 수량을 합친다."""
    if mode not in ("exact", "ignore_color"):
        raise ValueError("지원하지 않는 모드입니다. exact 또는 ignore_color를 선택하세요.")
    result: Counter[Key] = Counter()
    for (part, color), qty in counter.items():
        result[(part, color) if mode == "exact" else part] += qty
    return +result


def collection_have(
    conn: sqlite3.Connection, sets: list[tuple[str, int]],
    adjustments: Sequence[tuple[str, int, int]] = (),
) -> Counter[tuple[str, int]]:
    """전체 delta 합산 후 음수와 0인 키를 제거한다."""
    have: Counter[tuple[str, int]] = Counter()
    for number, qty in sets:
        if type(qty) is not int or qty < 1:
            raise ValueError("세트 수량은 1 이상의 정수여야 합니다.")
        for key, amount in set_parts(conn, number).items():
            have[key] += qty * amount
    for part, color, delta in adjustments:
        if not isinstance(part, str) or not part.strip() or type(color) is not int or type(delta) is not int:
            raise ValueError("부품 번호와 정수 색상·보정 수량을 확인하세요.")
        have[(part, color)] += delta
    return +have
