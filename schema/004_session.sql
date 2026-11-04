-- 004: 给 messages 加 session_id
--
-- 之前 messages 表没有会话维度，`_recent_turns` 读的是全表最近 N 条，
-- 于是：网页每次刷新生成新 session，但模型仍能看到上一次会话的内容，
-- 表现为"你刚才不是说过吗"这类错乱。
--
-- 旧数据统一标为 'legacy'，不丢，也不混进新会话。

ALTER TABLE messages ADD COLUMN session_id TEXT;

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);

UPDATE messages SET session_id = 'legacy' WHERE session_id IS NULL;

INSERT OR IGNORE INTO schema_meta (key, value) VALUES ('schema_version', '4');
