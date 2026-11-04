# companion — 单机自用情感陪伴 Agent

对应规格：[`../陪伴Agent架构-v2-决策收敛与落地规格.md`](../陪伴Agent架构-v2-决策收敛与落地规格.md)（v2.1）

当前进度：

- **M1 数据底座** —— §8 治理表 + §3.2 记忆时间字段 + §1.2 语汇索引 + FTS5 中文检索
- **M2 之 `query_time_resolver`** —— L0 同步时间解析（§3.3 / §3.4 全部规则）
- **M2 之 L1 召回** —— 三路候选、配额预留、`no_send` 全路径隔离、档案生日
- **FTS 四件套** —— 建表 / 写入同步 / 回填 / 一致性校验（脏标记触发器强制）
- **M3 删除链路** —— 事件簇圈定、三态（hide / blur / purge）、级联、`data_revision`
- **Prompt 拼装** —— 稳定前缀 / per-turn 动态段分离、分段预算、`untrusted_text()` 注入防护
- **真实 provider + 一轮对话** —— OpenAI 兼容流式、reasoning/usage 记账、端到端落库

`python -m pytest` → **96 项全绿**，无需安装任何依赖（真实调用走 `tools/live_smoke.py`）。

> 规格文档在父目录，**故意不进本仓库**：它是与评审方共享的单一事实源，拷进来就会分叉。

## 快速开始

```powershell
cd companion
python -m pytest                      # 96 项，不联网
python tools/live_smoke.py            # 真实 API 两轮对话（需要 .env）
python tools/live_smoke.py --cache    # 附带 prompt cache 门槛扫描
```

`.env`（已 gitignore）：

```
COMPANION_API_KEY=sk-...
COMPANION_BASE_URL=https://api.deepseek.com
COMPANION_MODEL=deepseek-flash
```

## 加密（已落地）

`db.connect(path, key_hex=...)` 走 SQLCipher（实测 4.19.0 community）：

- 加密文件里**没有明文、没有 `SQLite format 3` 头、没有 key 打不开**；
- 没有 `sqlcipher3` 绑定时**直接抛错，不静默降级为明文**；
- 密钥 32 字节随机值，落盘前用 **Windows DPAPI** 保护（绑当前用户）；
- 不传 `key_hex` 时是明文库，`security_mode()` 会如实报 `plaintext-dev`。

```powershell
pip install sqlcipher3

python tools/encrypt_db.py data/companion.db      # 明文 → 加密
python tools/encrypt_db.py data/companion.db --verify-only
```

```python
conn = db.connect_secure("data/companion.db")     # 自动建密钥 + DPAPI 保护
```

## 备份

**不要直接拷库文件**：DPAPI 绑机器，换机打不开；WAL 下拷贝还会不一致。

```python
crypto.export_encrypted(conn, "backup/2026-11-04.db", "你的口令")
crypto.restore_from_backup("backup/2026-11-04.db", "你的口令", "new.db", new_key)
```

备份用**口令**重新加密，因此换机可用；恢复时再用本机新 key 重新加密一遍。

两个 sqlcipher3 兼容性坑：Cursor 不接受 `sqlite3.Row`（要用各自的 Row）；
它抛自己的异常类，不是 `sqlite3.OperationalError`。

## prompt cache 实测（deepseek-flash, 2026-11-04）

| 前缀填充 | prompt | 首次命中 | 第二次命中 |
|---|---|---|---|
| 200 | 334 | 0 | 128 |
| 1200 | 1594 | 768 | **1408** |
| 3000 | 3826 | 2304 | 3584 |

缓存按 64 token 为块，**请求尾部约 200 token 永远不缓存**。前缀 1200 时第二次命中 88%。
结论：稳定前缀值得单独喂，但 200 token 级别基本命中不到——省 cache 不是初版的主要收益。

