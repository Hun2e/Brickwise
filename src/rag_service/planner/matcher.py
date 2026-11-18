"""색상별 또는 색상을 무시한 결정적 부족 분석."""

import sqlite3
from collections import Counter
from dataclasses import dataclass

from rag_service.config import load_settings
from .inventory import Key, rekey, set_parts
from .sets import SetInfo, get_set


class NoInventory(Exception):
    """분석에 필요한 부품 목록이 없는 세트."""

    def __init__(self, set_num: str):
        super().__init__("부품 목록이 없습니다. 부품 목록이 있는 다른 세트를 선택하세요.")
        self.set_num = set_num


@dataclass(frozen=True)
class MissingRow:
    part_num: str
    color_id: int | None
    part_name: str
    color_name: str | None
    need: int
    have: int
    missing: int


@dataclass(frozen=True)
class AnalysisResult:
    target: SetInfo
    mode: str
    need_total: int
    have_matched: int
    total_missing: int
    lots_missing: int
    coverage: float
    official_num_parts: int
    count_mismatch: bool
    rows: list[MissingRow]


def _missing(need: Counter[Key], have: Counter[Key], mode: str) -> Counter[Key]:
    """분석과 추천이 같은 Unknown 처리 및 수량 공식을 공유한다."""
    return Counter({key: qty - min(qty, max(0, have[key]))
                    if not (mode == "exact" and key[1] == -1) else qty
                    for key, qty in need.items()}) + Counter()


def analyze(
    conn: sqlite3.Connection, have: Counter[tuple[str, int]],
    target_set_num: str, mode: str | None = None,
) -> AnalysisResult:
    """부족한 키만 반환한다. 생략한 모드는 settings에서 읽는다."""
    mode = load_settings()["planner"]["default_mode"] if mode is None else mode
    available = rekey(have, mode)
    target = get_set(conn, target_set_num)
    need = rekey(set_parts(conn, target.set_num), mode)
    if not need:
        raise NoInventory(target.set_num)
    missing = _missing(need, available, mode)
    names = {(part, color): (name or part, color_name) for part, color, name, color_name in conn.execute(
        """SELECT f.part_num, f.color_id, p.name, c.name FROM set_parts_flat f
           LEFT JOIN parts p ON p.part_num = f.part_num
           LEFT JOIN colors c ON c.id = f.color_id WHERE f.set_num = ?""",
        (target.set_num,),
    )}
    part_names = {part: name for (part, _), (name, _) in names.items()}
    rows = []
    for key, amount in missing.items():
        part, color = key if mode == "exact" else (key, None)
        name, color_name = names[key] if mode == "exact" else (part_names[part], None)
        held = 0 if mode == "exact" and color == -1 else available[key]
        rows.append(MissingRow(part, color, name, color_name, need[key], held, amount))
    rows.sort(key=lambda row: (-row.missing, row.part_num, row.color_id if row.color_id is not None else -1))
    total, shortage = sum(need.values()), sum(missing.values())
    matched = total - shortage
    return AnalysisResult(target, mode, total, matched, shortage, len(missing),
                          matched / total if total else 0.0,
                          target.num_parts, target.num_parts != total, rows)
