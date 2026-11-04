-- 情感陪伴 Agent · M1 schema
-- 对应《陪伴Agent架构-v2-决策收敛与落地规格.md》v2.1 §1.2 / §3.2 / §4.3 / §8 / §9.1
--
-- 约定
--   * 所有时间列均为 TEXT，ISO-8601 UTC（YYYY-MM-DDTHH:MM:SSZ）；纯日期列用 YYYY-MM-DD。
--   * message_time  = 用户说这句话的时间（检索"昨天聊了什么"用它）
--     event_start  = 这件事发生的时间（检索"去年生日"用它）
--     created_at   = 记录入库的时间（异步/重试/导入会漂移，不得当说话时间用）
--   * 状态机：active / hidden / blurring / purge_pending / retired / superseded
--     其中 blurring / purge_pending 为中间态，检索必须排除。
--   * FTS 表存的是 bigram 预分词结果，不是原文；同步由 app 层 wrapper 维护，
--     没有触发器（SQLite 无法在触发器里做 CJK 分词）。

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ───────────────────────────────────────── 对话原文
CREATE TABLE IF NOT EXISTS messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL DEFAULT 1,
    role         TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    content      TEXT NOT NULL,
    message_time TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    turn_id      TEXT,
    no_send      INTEGER NOT NULL DEFAULT 0   -- 1 = 可本地检索，不得进入任何出网请求
);
CREATE INDEX IF NOT EXISTS idx_messages_time ON messages(message_time);
CREATE INDEX IF NOT EXISTS idx_messages_turn ON messages(turn_id);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(tok);

-- ───────────────────────────────────────── 长期记忆
CREATE TABLE IF NOT EXISTS memory_items (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id              INTEGER NOT NULL DEFAULT 1,
    category             TEXT NOT NULL DEFAULT '',
    content              TEXT NOT NULL,
    importance           INTEGER NOT NULL DEFAULT 1,
    source               TEXT NOT NULL DEFAULT 'extracted',

    -- §2.3 证据契约：助手输出不得独立作为用户事实的证据
    source_role          TEXT,
    epistemic_status     TEXT NOT NULL DEFAULT 'explicit'
                         CHECK (epistemic_status IN ('explicit', 'confirmed', 'inferred')),
    evidence_message_ids TEXT NOT NULL DEFAULT '[]',

    -- §3.2 槽位型事实的有效期（"我去年在哪工作"靠它回答）
    slot_key             TEXT,
    valid_from           TEXT,
    valid_to             TEXT,

    -- §3.1 双时间线
    message_time         TEXT,
    event_start          TEXT,
    event_end            TEXT,
    event_raw            TEXT,
    event_recurs         TEXT NOT NULL DEFAULT 'NONE'
                         CHECK (event_recurs IN ('NONE', 'YEARLY', 'MONTHLY')),
    event_tz             TEXT,
    date_conf            REAL,

    status               TEXT NOT NULL DEFAULT 'active'
                         CHECK (status IN ('active', 'hidden', 'blurring',
                                           'purge_pending', 'retired', 'superseded')),
    superseded_by        INTEGER REFERENCES memory_items(id),

    access_count         INTEGER NOT NULL DEFAULT 0,
    last_accessed        TEXT NOT NULL DEFAULT '',
    embedding            BLOB,

    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memory_status  ON memory_items(status, superseded_by);
CREATE INDEX IF NOT EXISTS idx_memory_event   ON memory_items(event_start);
CREATE INDEX IF NOT EXISTS idx_memory_msgtime ON memory_items(message_time);
CREATE INDEX IF NOT EXISTS idx_memory_slot    ON memory_items(slot_key, valid_from);
CREATE INDEX IF NOT EXISTS idx_memory_access  ON memory_items(last_accessed);

-- 同一槽位只允许一个"当前有效值"；旧值靠 valid_to 收口，不被删除
CREATE UNIQUE INDEX IF NOT EXISTS ux_memory_slot_current
    ON memory_items(slot_key)
    WHERE slot_key IS NOT NULL AND status = 'active' AND valid_to IS NULL;

CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(tok);

-- ───────────────────────────────────────── 关系语汇（§1.2）
CREATE TABLE IF NOT EXISTS lexicon_entry (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    surface              TEXT NOT NULL,
    canonical            TEXT,
    kind                 TEXT NOT NULL
                         CHECK (kind IN ('alias', 'inside_joke', 'nick_name', 'taboo')),
    meaning              TEXT NOT NULL,
    usage_scene          TEXT,
    usage_note           TEXT,
    confidence           REAL,
    first_seen           TEXT,
    last_used            TEXT,
    use_count            INTEGER NOT NULL DEFAULT 0,
    injected_count       INTEGER NOT NULL DEFAULT 0,  -- 被拼进 prompt 的次数（≠ 被模型采用）
    evidence_message_ids TEXT NOT NULL DEFAULT '[]',
    status               TEXT NOT NULL DEFAULT 'active'
                         CHECK (status IN ('active', 'retired')),
    superseded_by        INTEGER REFERENCES lexicon_entry(id)
);

-- 部分唯一索引：被取代的旧条目必须保留，全表唯一会阻止新增替代版本
CREATE UNIQUE INDEX IF NOT EXISTS idx_lexicon_active
    ON lexicon_entry(surface, kind)
    WHERE status = 'active' AND superseded_by IS NULL;

CREATE TABLE IF NOT EXISTS lexicon_candidate (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    surface              TEXT NOT NULL,
    kind                 TEXT NOT NULL
                         CHECK (kind IN ('alias', 'inside_joke', 'nick_name', 'taboo')),
    canonical            TEXT,
    meaning              TEXT,
    usage_scene          TEXT,
    usage_note           TEXT,
    occurrence_count     INTEGER NOT NULL DEFAULT 1,
    confidence           REAL NOT NULL DEFAULT 0.5,
    evidence_message_ids TEXT NOT NULL DEFAULT '[]',   -- §4.4 删除要反查
    first_seen           TEXT NOT NULL,
    last_seen            TEXT NOT NULL,
    status               TEXT NOT NULL DEFAULT 'pending'
                         CHECK (status IN ('pending', 'promoted', 'rejected'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_candidate_surface
    ON lexicon_candidate(surface, kind);

-- ───────────────────────────────────────── 治理底座（§8）
CREATE TABLE IF NOT EXISTS session_state (
    session_id             TEXT PRIMARY KEY,
    turn_index             INTEGER NOT NULL DEFAULT 0,
    topic_id               TEXT,
    affect_json            TEXT,
    locked_memory_ids      TEXT NOT NULL DEFAULT '[]',
    injected_lexicon_ids   TEXT NOT NULL DEFAULT '[]',
    recent_redirect_ids    TEXT NOT NULL DEFAULT '[]',
    last_recall_ids        TEXT NOT NULL DEFAULT '[]',
    updated_at             TEXT NOT NULL
);

-- 持久偏好（不进 session_state，重启与 /reset 都不清）
CREATE TABLE IF NOT EXISTS user_pref (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS async_task (
    task_id         TEXT PRIMARY KEY,
    turn_id         TEXT NOT NULL,
    kind            TEXT NOT NULL
                    CHECK (kind IN ('extract', 'validate', 'derive', 'curate', 'purge', 'blur')),
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'running', 'done', 'failed')),
    payload_ref     TEXT,
    idempotency_key TEXT UNIQUE,
    retry_count     INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_status ON async_task(status, created_at);

CREATE TABLE IF NOT EXISTS deletion_target (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    cluster_id   TEXT NOT NULL,
    target_kind  TEXT NOT NULL,   -- memory|message|diary|profile|kg|summary|lexicon|fts
    target_id    TEXT NOT NULL,
    is_exclusive INTEGER NOT NULL DEFAULT 1,
    deleted_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_deletion_cluster ON deletion_target(cluster_id);

CREATE TABLE IF NOT EXISTS memory_derivation (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id   INTEGER NOT NULL REFERENCES memory_items(id),
    target_kind TEXT NOT NULL,   -- diary|profile|kg_triple|summary
    target_id   TEXT NOT NULL,
    stale       INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_derivation_memory ON memory_derivation(memory_id);

-- 删除完成语义：所有在途请求 / 任务记录读取时的 revision，提交前校验
CREATE TABLE IF NOT EXISTS data_revision (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    revision   INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
INSERT OR IGNORE INTO data_revision (id, revision, updated_at)
    VALUES (1, 0, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'));

CREATE TABLE IF NOT EXISTS conversation_summary_state (
    user_id         INTEGER PRIMARY KEY,
    summary         TEXT NOT NULL DEFAULT '',
    last_message_id INTEGER NOT NULL DEFAULT 0,
    updated_at      TEXT NOT NULL
);

INSERT OR IGNORE INTO schema_meta (key, value) VALUES ('schema_version', '1');
