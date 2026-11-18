CREATE TABLE themes (id INTEGER PRIMARY KEY, name TEXT NOT NULL, parent_id INTEGER);
CREATE TABLE colors (
    id INTEGER PRIMARY KEY, name TEXT, rgb TEXT,
    is_trans INTEGER NOT NULL CHECK (is_trans IN (0, 1)),
    num_parts INTEGER, num_sets INTEGER, y1 INTEGER, y2 INTEGER
);
CREATE TABLE part_categories (id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE parts (part_num TEXT PRIMARY KEY, name TEXT, part_cat_id INTEGER, part_material TEXT);
CREATE TABLE part_relationships (rel_type TEXT, child_part_num TEXT, parent_part_num TEXT);
CREATE TABLE elements (element_id TEXT PRIMARY KEY, part_num TEXT, color_id INTEGER, design_id TEXT);
CREATE TABLE sets (
    set_num TEXT PRIMARY KEY, name TEXT, year INTEGER, theme_id INTEGER, num_parts INTEGER, img_url TEXT
);
CREATE TABLE minifigs (fig_num TEXT PRIMARY KEY, name TEXT, num_parts INTEGER, img_url TEXT);
CREATE TABLE inventories (id INTEGER PRIMARY KEY, version INTEGER NOT NULL, set_num TEXT NOT NULL);
CREATE TABLE inventory_parts (
    inventory_id INTEGER NOT NULL, part_num TEXT NOT NULL, color_id INTEGER NOT NULL,
    quantity INTEGER NOT NULL, is_spare INTEGER NOT NULL CHECK (is_spare IN (0, 1)), img_url TEXT
);
CREATE TABLE inventory_sets (inventory_id INTEGER, set_num TEXT, quantity INTEGER);
CREATE TABLE inventory_minifigs (inventory_id INTEGER, fig_num TEXT, quantity INTEGER);

-- 원본 참조 누락은 적재를 막지 않고 integrity.py에서 건수와 예시로 보고한다.
CREATE INDEX idx_inventory_parts_inventory ON inventory_parts(inventory_id);
CREATE INDEX idx_inventories_set ON inventories(set_num);
CREATE INDEX idx_sets_theme ON sets(theme_id);
CREATE INDEX idx_sets_year ON sets(year);

CREATE TABLE set_root_theme (
    set_num TEXT PRIMARY KEY, root_theme_id INTEGER, root_theme_name TEXT, subtheme_name TEXT
);
CREATE TABLE set_parts_flat (
    set_num TEXT NOT NULL, part_num TEXT NOT NULL, color_id INTEGER NOT NULL,
    quantity INTEGER NOT NULL, PRIMARY KEY (set_num, part_num, color_id)
);
CREATE INDEX idx_set_parts_flat_part_color ON set_parts_flat(part_num, color_id);
CREATE TABLE set_scope (
    set_num TEXT PRIMARY KEY, scope TEXT NOT NULL CHECK (scope IN ('core', 'compare_only'))
);
