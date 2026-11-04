# companion — 单机自用情感陪伴 Agent

对应规格：[`../陪伴Agent架构-v2-决策收敛与落地规格.md`](../陪伴Agent架构-v2-决策收敛与落地规格.md)（v2.1）

当前进度：**M1 数据底座**（§8 治理表 + §3.2 记忆时间字段 + §1.2 语汇索引 + FTS5 中文检索）。

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
src/companion/
  db.py                 连接 / PRAGMA / schema / FTS 同步 wrapper / 最小检索
  fts.py                CJK 分词（索引侧 unigram+bigram，查询侧 bigram）
  datewin.py            区间相交 与 跨年周年窗口（规格里最容易写错的两段 SQL）
tests/test_schema.py    10 项回归，覆盖评审提出的 4 个真实缺陷
```

## 已经被测试锁住的三条契约

1. **`bm25()` 是负值，越负越相关。** 直接当正分用会筛掉全部结果；
   `ORDER BY -bm25` 会把最差排最前。正确写法：`relevance = -bm25(...) DESC`。
2. **时间窗口用区间相交，不用 `BETWEEN`。** `event_start BETWEEN q_start AND q_end`
   会漏掉「3 月 1—10 日」在 3 月 5 日的查询。
3. **跨年周年窗口必须拆两段。** 月日字符串 `BETWEEN '12-28' AND '01-05'` 恒为假。

## 已知取舍

- FTS 表存的是分词结果，**没有触发器**（SQLite 触发器里做不了 CJK 分词），
  增删改必须走 `db.upsert_*_fts` / `db.delete_memory_fts`，并定期
  `db.rebuild_fts` 校验。这是规格里「四件套」的同步部分，回填与校验尚未实现。
- 单字查询靠索引侧 unigram 兜住，代价是索引体积约 2 倍。
