import sqlite3

from rag_service.services.catalog import list_sets


def test_scope_sort_alias_priority_and_labels():
    with sqlite3.connect(":memory:") as conn:
        conn.executescript("""
            CREATE TABLE sets(set_num TEXT, name TEXT, year INTEGER, num_parts INTEGER, img_url TEXT);
            CREATE TABLE set_scope(set_num TEXT, scope TEXT);
            CREATE TABLE set_entities(set_num TEXT, wiki_title TEXT);
            CREATE TABLE ko_aliases(alias TEXT, canonical_en TEXT, verified INTEGER);
            INSERT INTO sets VALUES ('75192-1', 'Millennium Falcon', 2017, 7541, 'image'),
                ('B-1', 'B', 2026, 200, ''), ('A-1', 'A', 2026, 100, NULL),
                ('C-1', 'C', 2020, 300, ''), ('outside-1', 'Outside', 2026, 1, '');
            INSERT INTO set_scope VALUES ('75192-1', 'compare_only'), ('B-1', 'core'), ('A-1', 'core'), ('C-1', 'core');
            INSERT INTO set_entities VALUES ('75192-1', 'Millennium Falcon');
            INSERT INTO ko_aliases VALUES ('가짜 후보', 'Millennium Falcon', 0), ('밀레니엄 팔콘', 'Millennium Falcon', 1);
        """)
        options = list_sets(conn)
        assert [o.set_num for o in options] == ["A-1", "B-1", "C-1", "75192-1"]
        assert options[-1].label() == "75192-1 · Millennium Falcon (2017) · 밀레니엄 팔콘 (비교용)"
        assert options[-1].aliases == ["밀레니엄 팔콘", "가짜 후보"]
        assert options[0].label() == "A-1 · A (2026)" and options[0].img_url == ""
        assert options[-1].num_parts == 7541
