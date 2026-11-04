-- 002: 长期档案（最小版）
--
-- §6.2 说长期档案最终要"权威进库、core.md 只作视图"，这里先落最小可用的
-- 结构化槽位表。当前只服务两件事：
--   1. birthday —— 让「去年生日」从 0.5 的年度粗窗口变成 0.9 的精确日；
--   2. calendar —— 农历 / 公历偏好，供 P2 的农历换算使用。
--
-- 不做自由文本：档案走白名单槽位，避免模型把"猜测"写成档案（§2.3）。

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS profile (
    key                  TEXT PRIMARY KEY,
    value                TEXT NOT NULL,
    evidence_message_ids TEXT NOT NULL DEFAULT '[]',
    updated_at           TEXT NOT NULL
);

INSERT OR IGNORE INTO schema_meta (key, value) VALUES ('schema_version', '2');
