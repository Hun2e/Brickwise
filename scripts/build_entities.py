"""오프라인 매핑 생성. 순서: load_rebrickable.py → build_entities.py → index_items.py."""

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rag_service.config import load_settings, resolve_path
from rag_service.data_prep.aliases import build_ko_aliases
from rag_service.data_prep.entities import build_set_entities, load_rules


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    try:
        settings = load_settings()
        rules, include_list = load_rules(resolve_path(settings["paths"]["entity_rules"]))
        path = resolve_path(settings["paths"]["sqlite"])
        with closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True)) as conn, conn:
            mapping = build_set_entities(conn, rules, include_list, settings["index"]["meta_scopes"])
            aliases = build_ko_aliases(conn, rules, resolve_path(settings["paths"]["ko_aliases_manual"]))
            by_scope = [dict(zip(("scope", "sets", "mapped", "rows"), row)) for row in conn.execute("""
                SELECT sc.scope, COUNT(DISTINCT sc.set_num), COUNT(DISTINCT e.set_num), COUNT(e.wiki_title)
                FROM set_scope sc LEFT JOIN set_entities e USING (set_num)
                GROUP BY sc.scope ORDER BY sc.scope""")]
        print(json.dumps({"mapping": mapping, "by_scope": by_scope, "aliases": aliases}, ensure_ascii=False))
        return 0
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
        print("매핑을 만들지 못했습니다. DB 적재와 규칙·별칭 설정을 확인하세요.")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
