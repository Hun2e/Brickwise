CREATE TABLE IF NOT EXISTS collections (id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS collection_sets (
    collection_id TEXT NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
    set_num TEXT NOT NULL,
    qty INTEGER NOT NULL CHECK (qty >= 1),
    PRIMARY KEY (collection_id, set_num)
);
CREATE TABLE IF NOT EXISTS collection_adjustments (
    id INTEGER PRIMARY KEY,
    collection_id TEXT NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
    part_num TEXT NOT NULL,
    color_id INTEGER NOT NULL,
    delta INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_adjustments_collection
    ON collection_adjustments(collection_id, id);

CREATE TABLE IF NOT EXISTS conversation_turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uid TEXT NOT NULL,
    thread TEXT NOT NULL,
    question TEXT NOT NULL,
    response TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_turns_user_thread
    ON conversation_turns(uid, thread, id);

-- 기존 테이블의 열 추가는 백업을 수행하는 migrations.migrate에서만 한다.
CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    uid TEXT NOT NULL,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    legacy_thread TEXT,
    UNIQUE (uid, legacy_thread)
);
CREATE INDEX IF NOT EXISTS idx_conversations_user ON conversations(uid, updated_at);
CREATE TABLE IF NOT EXISTS conversation_state (
    conversation_id TEXT PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
    state_json TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 0 CHECK (version >= 0),
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS turn_subjects (
    turn_id INTEGER NOT NULL REFERENCES conversation_turns(id) ON DELETE CASCADE,
    set_num TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('primary', 'compared', 'listed')),
    PRIMARY KEY (turn_id, set_num, role)
);
CREATE INDEX IF NOT EXISTS idx_subjects_set ON turn_subjects(set_num, role, turn_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_subjects_primary
    ON turn_subjects(turn_id) WHERE role = 'primary';
CREATE TABLE IF NOT EXISTS chat_requests (
    uid TEXT NOT NULL,
    request_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (status IN ('running', 'done', 'failed')),
    turn_id INTEGER REFERENCES conversation_turns(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (uid, request_id)
);
CREATE TABLE IF NOT EXISTS collection_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    collection_id TEXT NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
    request_id TEXT UNIQUE,
    set_num TEXT NOT NULL,
    op TEXT NOT NULL,
    qty_before INTEGER NOT NULL CHECK (qty_before >= 0),
    qty_after INTEGER NOT NULL CHECK (qty_after >= 0),
    created_at TEXT NOT NULL
);
