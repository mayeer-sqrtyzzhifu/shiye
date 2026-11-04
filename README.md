# companion — 单机自用情感陪伴 Agent

对应规格：[`../陪伴Agent架构-v2-决策收敛与落地规格.md`](../陪伴Agent架构-v2-决策收敛与落地规格.md)（v2.1）

当前进度：

- **M1 数据底座** —— §8 治理表 + §3.2 记忆时间字段 + §1.2 语汇索引 + FTS5 中文检索
- **M2 之 `query_time_resolver`** —— L0 同步时间解析（§3.3 / §3.4 全部规则）
- **M2 之 L1 召回** —— 三路候选（日期 / 记忆文本 / 原文）、配额预留、`no_send` 全路径隔离、档案生日
- **FTS 四件套** —— 建表 / 写入同步 / 回填 / 一致性校验（脏标记触发器强制）

`python -m pytest` → **56 项全绿**，无需安装任何依赖。

> 规格文档在父目录 [``../陪伴Agent架构-v2-决策收敛与落地规格.md``](../陪伴Agent架构-v2-决策收敛与落地规格.md)，**故意不进本仓库**：它是与评审方共享的单一事实源，拷进来就会分叉。

## 快速开始

```powershell
cd companion
python -m pytest          # 10 项测试，无需安装依赖
```

```python
from companion import db

conn = db.connect("data/companion.db")        # 明文，仅开发
db.apply_schema(conn)
print(db.security_mode(None))                  # "plaintext-dev"
print(db.integrity_report(conn))
```

## 加密边界（重要）

`db.connect(path, key_hex=...)` 会**强制**走 SQLCipher：

- 本机没有 `sqlcipher3` 绑定时**直接抛错，不静默降级为明文**；
- 不传 `key_hex` 时是明文库，`security_mode()` 会报 `plaintext-dev`。

Windows 上装 SQLCipher 通常要自己编 wheel，因此 M1 允许先跑明文开发；
**任何真实数据落盘前必须先解决加密**（规格 §9.1）。

## 目录

```
schema/001_init.sql     全部 DDL（可执行、已在 sqlite 3.51 验证）
schema/002_profile.sql  长期档案槽位（birthday / calendar / name / timezone）
schema/003_fts_sync.sql 脏标记触发器：绕过 wrapper 会被查出来
src/companion/
  db.py                 连接 / PRAGMA / 迁移 / FTS 同步与校验 / 文本检索
  fts.py                CJK 分词（索引侧 unigram+bigram，查询侧 bigram）
  datewin.py            区间相交 与 跨年周年窗口（规格里最容易写错的两段 SQL）
  timequery.py          query_time_resolver：双时间轴 + 三档置信 + 歧义保留
  recall.py             L1 三路召回与配额融合
tests/
  test_schema.py        10 项：DDL / 检索方向 / 索引语义
  test_timequery.py     24 项：时间解析规则
  test_recall.py        12 项：三路融合、配额、no_send、档案生日
  test_fts_sync.py      10 项：脏标记 / 回填 / 一致性校验
tools/check_fts.py      巡检与修复 CLI
```

## FTS 一致性

SQLite 触发器里做不了 CJK 分词，所以索引不能由触发器直接维护。折中方案是
**触发器只标脏**：任何对 `memory_items` / `messages` 的写入都留下一条
`fts_dirty`，而真正的分词与写索引仍然在 Python 侧。

于是形成闭环：

| 做法 | 结果 |
|---|---|
| 走 `db.upsert_*_fts` | 写索引 + 清脏标记 → `verify_fts().ok == True` |
| 绕过 wrapper 直接写 | 脏标记留下 → `verify_fts()` 报 `missing` / `stale` |
| 直接删主表 | 索引残影 → 报 `orphan` |

```powershell
python tools/check_fts.py data/companion.db --fix
```

`orphan`（主表已删、索引还在）**不会真的被召回**，因为检索 SQL 与主表 JOIN
——但那只是第二道防线，残影仍会撑大索引，并让任何不走 JOIN 的路径漏出已删内容。

## L1 召回

```python
from companion.recall import recall
r = recall(conn, "昨天我们聊了什么", reference_time="2026-11-04T09:00:00Z")
r.query.axis          # "message"
[(h.source, h.id) for h in r.hits]        # [("date_message", 1)]
[h.id for h in r.local_only]              # no_send 的命中只在这里，不进上下文
r.notes                                   # {"tier0": 1, "memory": 0, ...}
```

配额：`tier0 ≤ 2 / 记忆文本 ≤ 3 / 原文 ≤ 1`，总量 ≤5。**原文那 1 个位置先预留**——
否则 tier0 和记忆文本会把 5 个位置吃光，原文路永远命中不到（这解决了
"三路 2/3/1 相加是 6、截到 5 会随机挤掉一路"的问题）。

## `query_time_resolver` 的行为

```python
from companion.timequery import resolve
q = resolve("昨天我们聊了什么", reference_time="2026-11-04T09:00:00Z")
q.axis        # "message" —— 查消息时间轴，不是事件轴
q.conf        # 0.95
q.exact.start # "2026-11-03"
q.usable      # True
```

三档阈值（`0.6` / `0.35`）：

| 置信 | 行为 |
|---|---|
| ≥ 0.6 | 精确日 + 扩窗（±7 天） |
| 0.35 – 0.6 | 只给粗窗口（月 / 年范围） |
| < 0.35 | **不查日期路**，只走文本召回 |

两条容易做错的规则：

- 相对时间锚定**源消息时间**，不是 `now()`；
- 「3 月 5 号」这种缺年份的表达**保留歧义**，两个候选窗口都返回（从 2026-11 看，最近的是 2027-03-05，不是 2026-03-05）。

## 已经被测试锁住的三条契约

1. **`bm25()` 是负值，越负越相关。** 直接当正分用会筛掉全部结果；
   `ORDER BY -bm25` 会把最差排最前。正确写法：`relevance = -bm25(...) DESC`。
2. **时间窗口用区间相交，不用 `BETWEEN`。** `event_start BETWEEN q_start AND q_end`
   会漏掉「3 月 1—10 日」在 3 月 5 日的查询。
3. **跨年周年窗口必须拆两段。** 月日字符串 `BETWEEN '12-28' AND '01-05'` 恒为假。

## 已知取舍

- FTS 表存的是分词结果，没有触发器分词，**增删改必须走 `db.upsert_*_fts` /
  `db.delete_memory_fts`**。绕过去不会静默：`fts_dirty` 会留下记录，
  `verify_fts()` 与 `tools/check_fts.py` 都能查出来。
- 单字查询靠索引侧 unigram 兜住，代价是索引体积约 2 倍。
