"""문서·섹션 라벨을 채점하는 도메인 독립 순수 함수."""

from collections import defaultdict
from statistics import mean
from typing import Literal

Level = Literal["section", "doc"]


def label_hit(card_meta: dict, relevant: list[dict], level: Level = "section") -> bool:
    """섹션은 정답 경로의 접두어로, 문서는 ID로만 비교한다."""
    if level not in ("section", "doc"):
        raise ValueError("지원하지 않는 라벨 수준입니다.")
    return any(card_meta.get("doc_id") == label["doc_id"] and
               (level == "doc" or card_meta.get("section_path", "").startswith(label["section_path"]))
               for label in relevant)


def recall_at_k(ranked: list[dict], relevant: list[dict], k: int, level: Level = "section") -> float:
    """중복 청크·중복 정답은 분모나 적중 수를 늘리지 않는다."""
    if k < 0:
        raise ValueError("k는 음수일 수 없습니다.")
    if level not in ("section", "doc"):
        raise ValueError("지원하지 않는 라벨 수준입니다.")
    labels = {(row["doc_id"], row["section_path"] if level == "section" else "") for row in relevant}
    if not labels:
        return 0.0
    hits = sum(any(label_hit(card, [{"doc_id": doc, "section_path": section}], level)
                   for card in ranked[:k]) for doc, section in labels)
    return hits / len(labels)


def mrr(ranked: list[dict], relevant: list[dict], level: Level = "section") -> float:
    """첫 정답 청크 순위의 역수. 없으면 0."""
    if level not in ("section", "doc"):
        raise ValueError("지원하지 않는 라벨 수준입니다.")
    return next((1 / rank for rank, card in enumerate(ranked, 1) if label_hit(card, relevant, level)), 0.0)


def summarize(rows: list[dict], by: list[str]) -> dict:
    """rows의 metrics를 문항 단위 매크로 평균하고 각 속성별로 나눈다."""
    def aggregate(group: list[dict]) -> dict:
        names = sorted({key for row in group for key in row["metrics"]})
        return {"count": len(group), "metrics": {
            name: mean(row["metrics"][name] for row in group) for name in names}}

    result = {"overall": aggregate(rows)}
    for field in by:
        groups: dict[str, list[dict]] = defaultdict(list)
        for row in rows:
            groups[str(row[field])].append(row)
        result[field] = {key: aggregate(group) for key, group in sorted(groups.items())}
    return result
