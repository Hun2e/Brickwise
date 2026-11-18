"""보유 3세트 기준 추천 시간을 측정한다. 네트워크와 비밀 파일은 사용하지 않는다."""

import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from statistics import mean
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rag_service.config import load_settings, resolve_path
from rag_service.planner import collection_have, recommend


def main() -> int:
    """각 범위를 5회 실행하여 평균과 개별 시간을 출력한다."""
    settings = load_settings()
    path = resolve_path(settings["paths"]["sqlite"])
    if not path.is_file():
        print("DB가 없습니다. Rebrickable 적재를 먼저 실행하세요.")
        return 1
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
        owned = {r[0] for r in conn.execute("SELECT set_num FROM set_scope WHERE scope='core' ORDER BY set_num LIMIT 3")}
        if len(owned) != 3:
            print("성능 측정에는 core 세트가 3개 이상 필요합니다.")
            return 1
        have = collection_have(conn, [(number, 1) for number in sorted(owned)])
        print("Source: Rebrickable (https://rebrickable.com/downloads/)")
        print("Owned: " + ", ".join(sorted(owned)))
        for scope in (["core"], ["core", "compare_only"]):
            timings = []
            for _ in range(5):
                started = perf_counter()
                result = recommend(conn, have, owned, scope=scope)
                timings.append(perf_counter() - started)
            print(f"scope={scope}: mean={mean(timings):.4f}s; runs="
                  + ", ".join(f"{value:.4f}" for value in timings)
                  + f"; recommendations={len(result)}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, sqlite3.Error):
        print("측정 실패: DB 적재 상태와 planner 설정을 확인하세요.", file=sys.stderr)
        raise SystemExit(1) from None
