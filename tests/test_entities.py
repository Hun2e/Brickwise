"""규칙·별칭의 결정성과 실데이터 매핑 회귀 검사."""

import sqlite3
from dataclasses import replace

import pytest
import yaml

from rag_service.config import load_settings, resolve_path
from rag_service.data_prep.aliases import aliases_for, build_ko_aliases
from rag_service.data_prep.entities import build_set_entities, load_rules, match_entities


def rules_config():
    return load_rules(resolve_path(load_settings()["paths"]["entity_rules"]))


def populate_mapping(conn):
    settings = load_settings()
    rules, include_list = rules_config()
    build_set_entities(conn, rules, include_list, settings["index"]["meta_scopes"])
    return build_ko_aliases(conn, rules, resolve_path(settings["paths"]["ko_aliases_manual"]))


@pytest.fixture
def real_db():
    """原 DB는 읽기 전용으로 연결하고 테스트 파생 테이블은 메모리에만 만든다."""
    path = resolve_path(load_settings()["paths"]["sqlite"])
    if not path.is_file():
        pytest.skip("data/lego.sqlite가 없습니다.")
    conn = sqlite3.connect(":memory:", uri=True)
    conn.executescript(resolve_path("src/rag_service/db/schema.sql").read_text(encoding="utf-8"))
    conn.execute("ATTACH DATABASE ? AS source", (path.as_uri() + "?mode=ro",))
    for table in ("set_scope", "themes", "minifigs"):
        conn.execute(f"INSERT INTO {table} SELECT * FROM source.{table}")
    for table in ("sets", "set_root_theme", "inventories"):
        conn.execute(f"INSERT INTO {table} SELECT * FROM source.{table} WHERE set_num IN (SELECT set_num FROM set_scope)")
    conn.execute("""INSERT INTO inventory_minifigs SELECT * FROM source.inventory_minifigs
        WHERE inventory_id IN (SELECT id FROM inventories)""")
    conn.commit()
    conn.execute("DETACH DATABASE source")
    populate_mapping(conn)
    try:
        yield conn
    finally:
        conn.close()


@pytest.mark.parametrize("name, expected", [
    ("Luke Skywalker's X-Wing Fighter", ["X-wing fighter", "Luke Skywalker"]),
    ("MILLENNIUM FALCON", ["Millennium Falcon"]),
    ("Republic Gunship", []), ("Hoth", ["Hoth"]), ("Hothouse", []),
])
def test_matching(name, expected):
    rules, include_list = rules_config()
    assert not include_list
    assert [r.wiki_title for r in match_entities(name, rules, include_list)] == expected


def test_list_switch_and_duplicate_order():
    rules, _ = rules_config()
    assert match_entities("B-wing U-wing", rules, False) == []
    matched = match_entities("B-wing U-wing", rules, True)
    assert [r.wiki_title for r in matched] == ["List of Star Wars starfighters"]
    assert match_entities("B-wing", matched + [replace(matched[0], index=999)], True) == matched


def test_real_core_mapping_and_scope_rebuild(real_db):
    counts = real_db.execute("""SELECT COUNT(e.wiki_title) FROM set_scope s
        LEFT JOIN set_entities e ON s.set_num=e.set_num WHERE s.scope='core'
        GROUP BY s.set_num""").fetchall()
    assert len(counts) == 332
    assert abs(sum(count > 0 for (count,) in counts) - 249) <= 5
    assert max(count for (count,) in counts) == 3
    assert abs(sum(count for (count,) in counts) / len(counts) - 0.93) < 0.05
    rules, include_list = rules_config()
    first = build_set_entities(real_db, rules, include_list, ["core", "compare_only"])
    assert first == build_set_entities(real_db, rules, include_list, ["core", "compare_only"])
    assert first["sets"] == 378
    build_set_entities(real_db, rules, include_list, ["core"])
    assert real_db.execute("""SELECT COUNT(*) FROM set_entities e JOIN set_scope s
        ON e.set_num=s.set_num WHERE s.scope='compare_only'""").fetchone()[0] == 0
    assert build_set_entities(real_db, rules, include_list, []) == {"sets": 0, "mapped": 0, "rows": 0}


def test_alias_sources_and_idempotency(real_db):
    assert populate_mapping(real_db) == {"wiki_langlink": 45, "manual": 6}
    assert populate_mapping(real_db) == {"wiki_langlink": 45, "manual": 6}
    assert aliases_for(real_db, ["Millennium Falcon"] * 2) == (["밀레니엄 팰컨"], ["밀팔", "팔콘"])
    assert aliases_for(real_db, []) == ([], [])
    assert real_db.execute("SELECT COUNT(*) FROM ko_aliases WHERE source='manual' AND verified=1").fetchone()[0] == 0


def test_alias_collision_verification_and_removed_manual(tmp_path):
    with sqlite3.connect(":memory:") as conn:
        rules, _ = rules_config()
        path = tmp_path / "aliases.yaml"
        rows = [dict(alias="밀레니엄 팰컨", canonical_en="Millennium Falcon", kind="vehicle", verified=0),
                dict(alias="검수 별칭", canonical_en="Millennium Falcon", kind="vehicle", verified=1)]
        path.write_text(yaml.safe_dump({"aliases": rows + rows}), encoding="utf-8")
        assert build_ko_aliases(conn, rules, path) == {"wiki_langlink": 45, "manual": 1}
        assert aliases_for(conn, ["Millennium Falcon"]) == (["검수 별칭", "밀레니엄 팰컨"], [])
        path.write_text("aliases: []", encoding="utf-8")
        assert build_ko_aliases(conn, rules, path)["manual"] == 0
        assert aliases_for(conn, ["Millennium Falcon"]) == (["밀레니엄 팰컨"], [])
