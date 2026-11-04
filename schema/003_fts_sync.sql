-- 003: FTS 同步的强制约束
--
-- SQLite 触发器里做不了 CJK 分词，所以 FTS 索引无法由触发器直接维护。
-- 但触发器**可以**标记脏行：任何对主表的写入都留下一条 fts_dirty 记录。
--
-- 于是形成闭环：
--   * 走 wrapper（db.upsert_*_fts）→ 写索引 + 清脏标记 → verify 通过；
--   * 绕过 wrapper 直接写 → 脏标记留下 → verify 失败、backfill 修复。
--
-- 这就是「增删改同步」的强制约束：不靠自觉，靠能查出来。

CREATE TABLE IF NOT EXISTS fts_dirty (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    table_name  TEXT NOT NULL,
    rowid_value INTEGER NOT NULL,
    changed_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fts_dirty_target ON fts_dirty(table_name, rowid_value);

-- memory_items
CREATE TRIGGER IF NOT EXISTS trg_memory_ai AFTER INSERT ON memory_items BEGIN
    INSERT INTO fts_dirty (table_name, rowid_value, changed_at)
    VALUES ('memory_items', NEW.id, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'));
END;

CREATE TRIGGER IF NOT EXISTS trg_memory_au AFTER UPDATE OF content ON memory_items BEGIN
    INSERT INTO fts_dirty (table_name, rowid_value, changed_at)
    VALUES ('memory_items', NEW.id, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'));
END;

CREATE TRIGGER IF NOT EXISTS trg_memory_ad AFTER DELETE ON memory_items BEGIN
    INSERT INTO fts_dirty (table_name, rowid_value, changed_at)
    VALUES ('memory_items', OLD.id, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'));
END;

-- messages
CREATE TRIGGER IF NOT EXISTS trg_message_ai AFTER INSERT ON messages BEGIN
    INSERT INTO fts_dirty (table_name, rowid_value, changed_at)
    VALUES ('messages', NEW.id, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'));
END;

CREATE TRIGGER IF NOT EXISTS trg_message_au AFTER UPDATE OF content ON messages BEGIN
    INSERT INTO fts_dirty (table_name, rowid_value, changed_at)
    VALUES ('messages', NEW.id, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'));
END;

CREATE TRIGGER IF NOT EXISTS trg_message_ad AFTER DELETE ON messages BEGIN
    INSERT INTO fts_dirty (table_name, rowid_value, changed_at)
    VALUES ('messages', OLD.id, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'));
END;

INSERT OR IGNORE INTO schema_meta (key, value) VALUES ('schema_version', '3');
