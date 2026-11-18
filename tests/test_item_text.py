"""최신 인벤토리·공식 수치·짧은 이름으로 생성한 항목 텍스트 검사."""

import sqlite3

import pytest

from rag_service.config import resolve_path
from rag_service.data_prep.item_text import build_item_doc, iter_item_docs, short_minifig_name
from test_entities import populate_mapping, real_db


@pytest.mark.parametrize("original, short", [
    ("Chewbacca, Dark Brown - Long Lines on Legs on Inside", "Chewbacca"),
    ("Astromech Droid, R2-D2, Flat Silver Dome, Dark Blue Print with Lavender Dots", "R2-D2"),
    ("Lando Calrissian, Old, Dark Azure and Black Cape", "Lando Calrissian"),
    ("Clone Trooper, Phase 2", "Clone Trooper"), ("Clone Trooper", "Clone Trooper"),
    ("Battle Droid, Dark Orange, One Straight Arm", "Battle Droid"),
    ("Battle Droid, Two Bent Arms", "Battle Droid"),
    ("Stormtrooper, First Order - Pointed Mouth", "Stormtrooper"),
    ("Astromech Droid, Dark Green Head, New Republic", "Astromech Droid"),
    ("Clone Trooper, 501st Legion, White Arms", "Clone Trooper"),
    ("Protocol Droid, C-3PO, Gold", "C-3PO"),
    ("Droid, R1-J5 (Bucket)", "R1-J5 (Bucket)"),
    ("Droid, ", "Droid"),
])
def test_short_names(original, short):
    assert short_minifig_name(original) == short


@pytest.mark.parametrize("original, short", [
    ("Astromech Droid, R2-D2, Flat Silver Dome, Dark Blue Print with Lavender Dots", "R2-D2"),
    ("Boolio", "Boolio"),
    ("C-3PO, Pearl Gold, Colorful Wires, Printed Legs", "C-3PO"),
    ("Chewbacca, Dark Brown - Long Lines on Legs on Inside", "Chewbacca"),
    ("Finn, Tan Undershirt, Dark Blue Legs", "Finn"),
    ("Lando Calrissian, Old, Dark Azure and Black Cape", "Lando Calrissian"),
    ("Astromech Droid, R2-Q2 with Large Red Dots", "R2-Q2"),
    ("Astromech Droid, R2-D2, Flat Silver Dome, Dark Blue Print with Dark Pink Dots", "R2-D2"),
    ("Astromech Droid, R3-S1", "R3-S1"),
    ("Astromech Droid, C1-10P (Chopper), Light Bluish Gray", "C1-10P (Chopper)"),
    ("Battle Droid, Two Bent Arms, Light Bluish Grey Clip", "Battle Droid"),
    ("Battle Droid, Two Bent Arms, 1 x 2 Plate on Back", "Battle Droid"),
    ("Battle Droid, Dark Orange, One Straight Arm, One Bent Arm", "Battle Droid"),
    ("Battle Droid, Kashyyyk", "Battle Droid"),
    ("Clone Trooper, 501st Legion, White Arms", "Clone Trooper"),
    ("Clone Trooper, 41st Elite Corps Scout Battalion Armor (Kashyyyk)", "Clone Trooper"),
    ("Clone Trooper, Airborne, Orange Markings", "Clone Trooper"),
    ("Clone Trooper, 212th Attack Battalion, Orange Markings", "Clone Trooper"),
    ("Stormtrooper, Dark Bluish Gray Squares on Back of Helmet", "Stormtrooper"),
    ("Stormtrooper, Dark Azure Helmet Vents, Closed Mouth (Rebels)", "Stormtrooper"),
    ("Stormtrooper, First Order - Pointed Mouth", "Stormtrooper"),
    ("Stormtrooper, Artillery", "Stormtrooper"),
    ("Scout Trooper - Light Nougat Head, Black Arms", "Scout Trooper"),
    ("Mandalorian Loyalist", "Mandalorian Loyalist"),
    ("Stormtrooper, Dark Azure Helmet Vents, Angry (Rebels)", "Stormtrooper"),
    ("AT-DP Pilot", "AT-DP Pilot"),
    ("Mace Windu - White Arm Print", "Mace Windu"),
    ("Mouse Droid (MSE-6-series Repair Droid), Clip on Top", "Mouse Droid (MSE-6-series Repair Droid)"),
    ("Jawa, Black Strap over Right Shoulder", "Jawa"),
    ("Weequay Guard", "Weequay Guard"),
])
def test_pm_feedback_original_samples(original, short):
    """기존 표본의 정상 27개와 수정 대상 3개의 기대값을 고정한다."""
    assert short_minifig_name(original) == short


@pytest.mark.parametrize("name", ["R2-D2", "C-3PO", "K-2SO", "AT-DP Pilot"])
def test_name_internal_hyphens_are_preserved(name):
    assert short_minifig_name(name) == name
    assert short_minifig_name(f"{name} - White Arm Print") == name


@pytest.mark.parametrize("original, short", [
    ("Droid - R2-Q2 with Large Red Dots", "R2-Q2"),
    ("Mace Windu with White Arm Print", "Mace Windu"),
    ("Clone Trooper - Phase 2 with White Arms", "Clone Trooper"),
])
def test_descriptions_removed_before_variant_check(original, short):
    assert short_minifig_name(original) == short


def test_prefixes_are_configurable():
    assert short_minifig_name("Astromech Droid, R2-D2", []) == "Astromech Droid"
    assert short_minifig_name("Robot, C-3PO", ["Robot"]) == "C-3PO"


def test_real_falcon_doc(real_db):
    doc = build_item_doc(real_db, "75257-1")
    assert "Millennium Falcon (75257-1)" in doc.text
    assert "공식 부품 수: 1,328개" in doc.text
    assert "별칭: 밀레니엄 팰컨" in doc.text
    assert "검색어: 밀팔, 팔콘" in doc.text
    assert "미니피그(6): Boolio, C-3PO, Chewbacca, Finn, Lando Calrissian, R2-D2" in doc.text
    assert "같은 기체 다른 판본" not in doc.text
    assert "Flat Silver Dome" not in str(doc)
    assert doc.metadata == {"source_type": "set_meta", "set_num": "75257-1",
        "name": "Millennium Falcon", "year": 2019, "theme": "Star Wars", "num_parts": 1328,
        "num_minifigs": 6, "scope": "core", "has_ko_alias": True, "entities": ["Millennium Falcon"]}
    docs = list(iter_item_docs(real_db, ["core", "compare_only"]))
    assert len(docs) == len({doc.set_num for doc in docs}) == 378
    assert list(iter_item_docs(real_db, [])) == []


def test_latest_inventory_quantity_dedup_and_no_entity():
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(resolve_path("src/rag_service/db/schema.sql").read_text(encoding="utf-8"))
        conn.execute("INSERT INTO themes VALUES (1, 'Star Wars', NULL)")
        conn.execute("INSERT INTO sets VALUES ('test-1', 'Republic Gunship', 2020, 1, 100, '')")
        conn.execute("INSERT INTO set_scope VALUES ('test-1', 'core')")
        # ID 크기보다 version이 우선이고 같은 version이면 가장 큰 ID를 쓴다.
        conn.executemany("INSERT INTO inventories VALUES (?, ?, 'test-1')", [(99, 1), (1, 2), (2, 2)])
        conn.executemany("INSERT INTO minifigs VALUES (?, ?, 1, '')",
                         [('old', 'Old figure'), ('a', 'Zebra, White'), ('b', 'Zebra, Black'), ('c', 'Alpha')])
        conn.executemany("INSERT INTO inventory_minifigs VALUES (?, ?, ?)",
                         [(99, 'old', 99), (1, 'old', 99), (2, 'a', 2), (2, 'b', 3), (2, 'c', 1)])
        populate_mapping(conn)
        doc = build_item_doc(conn, "test-1")
        assert "미니피그(6): Alpha, Zebra" in doc.text
        assert "Old figure" not in doc.text and "관련:" not in doc.text
        assert "별칭:" not in doc.text and "검색어:" not in doc.text
        assert "entities" not in doc.metadata and not doc.metadata["has_ko_alias"]
        conn.execute("DELETE FROM inventory_minifigs")
        assert "미니피그(0): 없음" in build_item_doc(conn, "test-1").text
        with pytest.raises(ValueError, match="범위에 없는"):
            build_item_doc(conn, "missing")
    finally:
        conn.close()
