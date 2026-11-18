"""DB·planner 결과를 수치 검증 가능한 한국어 근거로 변환한다."""

from rag_service.db.queries import FilterSpec, SetFact, SetOps, VersionCompare
from rag_service.planner.matcher import AnalysisResult
from rag_service.planner.recommend import Recommendation
from .evidence import Card


def _card(title: str, numbers: list[str], lines: list[str], n: int) -> Card:
    numbers = list(dict.fromkeys(numbers))
    url = f"https://rebrickable.com/sets/{numbers[0]}/" if numbers else "https://rebrickable.com/downloads/"
    return Card(n=n, source_type="db", title=title, url=url, license="Rebrickable",
                text="\n".join(lines), item_id=",".join(numbers), lang="ko")


def _counts(official: int, aggregate: int) -> str:
    text = f"부품 수(Rebrickable 기준): {official:,}개"
    if official != aggregate:
        text += f" (부품 목록 집계 {aggregate:,}개 — 원본 불일치)"
    return text


def minifig_text(f: SetFact) -> str:
    """이름별 수량을 보존하는 표시 문자열."""
    names = ", ".join(f"{name} × {qty:,}" for name, qty in f.minifigs) or "없음"
    return f"미니피그({sum(qty for _, qty in f.minifigs):,}): {names}"


def _fact_lines(f: SetFact) -> list[str]:
    return [f"[DB] 세트: {f.set_num} {f.name}", _counts(f.num_parts, f.agg_parts),
            f"출시 연도: {f.year}", f"서브테마: {f.subtheme}", minifig_text(f),
            f"범위: {f.scope or '범위 밖'}"]


def fact_card(f: SetFact, n: int) -> Card:
    return _card(f"세트 정보 · {f.set_num}", [f.set_num], _fact_lines(f), n)


def set_ops_card(r: SetOps, n: int) -> Card:
    lines = [f"[DB] 부품 비교: {r.a} vs {r.b}", f"비교 모드: {r.mode}",
             f"공통 종류: {r.common_lots:,}종", f"공통 수량: {r.common_qty:,}개",
             f"A만 종류: {r.only_a_lots:,}종", f"A만 수량: {r.only_a_qty:,}개",
             f"B만 종류: {r.only_b_lots:,}종", f"B만 수량: {r.only_b_qty:,}개",
             f"색상 무시 공통 종류: {r.common_lots_ignore_color:,}종",
             "집계 기준: set_parts_flat (스페어 제외·미니피그 전개)",
             "단독 기준: 상대에 없는 키의 수량 (공통 키의 수량 차이 제외)"]
    lines.extend(f"공통 부품: {part} · {name} · 색상 {color if color is None else format(color, ',')} · {qty:,}개"
                 for part, color, name, qty in r.top_common)
    for fact in r.facts:
        lines.extend(_fact_lines(fact))
    return _card(f"부품 비교 · {r.a} vs {r.b}", [r.a, r.b], lines, n)


def compare_card(r: VersionCompare, n: int) -> Card:
    lines = [f"[DB] 판본 비교: {r.entity}", f"비교 판본: {len(r.rows):,}개"]
    numbers = []
    for row in r.rows:
        ids = row["set_nums"]
        numbers.extend(ids)
        count = row["minifig_count"]
        count_text = f"{count:,}" if isinstance(count, int) else count
        mismatches = [f"{number}: 부품 목록 집계 {total:,}개 — 원본 불일치"
                      for number, total in row["agg_parts"].items() if total != row["num_parts"]]
        note = " (" + "; ".join(mismatches) + ")" if mismatches else ""
        lines.append(f"세트: {row['set_num']} · 출시 연도: {row['year']} · "
                     f"부품 수(Rebrickable 기준): {row['num_parts']:,}개 · 미니피그: {count_text}{note}")
    lines.append(f"기타 변형 {len(r.excluded):,}개 (비교 대상 제외)")
    return _card(f"판본 비교 · {r.entity}", numbers, lines, n)


def analysis_card(r: AnalysisResult, n: int) -> Card:
    lines = [f"[DB] 제작 분석: {r.target.set_num} {r.target.name}", f"비교 모드: {r.mode}",
             _counts(r.official_num_parts, r.need_total), f"필요 수량: {r.need_total:,}개",
             f"보유 일치 수량: {r.have_matched:,}개", f"부족 수량: {r.total_missing:,}개",
             f"부족 종류: {r.lots_missing:,}종", f"커버리지: {r.coverage * 100:,.1f}%"]
    for row in r.rows:
        color = "색상 무시" if row.color_id is None else f"{row.color_name or '미상'} ({row.color_id:,})"
        lines.append(f"부족 부품: {row.part_num} · {row.part_name} · {color} · "
                     f"필요 {row.need:,} · 보유 {row.have:,} · 부족 {row.missing:,}")
    return _card(f"제작 분석 · {r.target.set_num}", [r.target.set_num], lines, n)


def recommend_card(recs: list[Recommendation], mode: str, n: int) -> Card:
    lines = ["[DB] 제작 추천", f"비교 모드: {mode}", f"추천 결과: {len(recs):,}개"]
    for r in recs:
        info = r.set_info
        lines.extend([f"세트: {info.set_num} {info.name}", f"출시 연도: {info.year}",
                      _counts(info.num_parts, r.need_total), f"부족 수량: {r.total_missing:,}개",
                      f"부족 종류: {r.lots_missing:,}종", f"커버리지: {r.coverage * 100:,.1f}%"])
    return _card("제작 추천", [r.set_info.set_num for r in recs], lines, n)


def filter_card(results: list[SetFact], total: int, spec: FilterSpec, n: int) -> Card:
    lines = ["[DB] 조건 검색", f"전체 결과: {total:,}개", f"표시 결과: {len(results):,}개",
             f"정렬: {spec.sort} {spec.order}", f"범위: {', '.join(spec.scopes)}"]
    for label, value, fmt in (("연도 하한", spec.year_from, ""), ("연도 상한", spec.year_to, ""),
                              ("부품 수 하한", spec.parts_min, ","), ("부품 수 상한", spec.parts_max, ",")):
        if value is not None:
            lines.append(f"{label}: {value:{fmt}}")
    for label, value in (("서브테마", spec.subtheme), ("미니피그 이름", spec.minifig),
                         ("미니피그 포함", spec.has_minifig), ("이름 키워드", ", ".join(spec.name_keywords))):
        if value is not None:
            lines.append(f"{label}: {value}")
    for fact in results:
        lines.extend(_fact_lines(fact))
    return _card("조건 검색 결과", [f.set_num for f in results], lines, n)
