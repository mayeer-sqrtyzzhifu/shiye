# 拾夜 — 单机自用情感陪伴 Agent

对应规格：[`../陪伴Agent架构-v2-决策收敛与落地规格.md`](../陪伴Agent架构-v2-决策收敛与落地规格.md)（v2.1）

## 一键启动

**双击 `启动 拾夜.cmd`** —— 检查 python、启动服务、自动打开浏览器；
出错时停住窗口并给出原因（端口被占 / 缺依赖）。

浏览器地址是 <http://127.0.0.1:8765/>。**直接双击 `web/index.html` 是连不上的**，
那是 `file://`，页面会自己提示你去点 http 地址。

## 当前进度

- **M1 数据底座** —— §8 治理表 + §3.2 记忆时间字段 + §1.2 语汇索引 + FTS5 中文检索
- **M2 之 `query_time_resolver`** —— L0 同步时间解析（§3.3 / §3.4 全部规则）
- **M2 之 L1 召回** —— 三路候选、配额预留、`no_send` 全路径隔离、档案生日、alias 归一
- **FTS 四件套** —— 建表 / 写入同步 / 回填 / 一致性校验（脏标记触发器强制）
- **M3 删除链路** —— 事件簇圈定、三态（hide / blur / purge）、级联、`data_revision`
- **Prompt 拼装** —— 稳定前缀 / per-turn 动态段分离、分段预算、`untrusted_text()` 注入防护
- **真实 provider + 一轮对话** —— OpenAI 兼容流式、reasoning/usage 记账、端到端落库
- **SQLCipher 加密与备份** —— DPAPI 保护密钥、口令备份、明文库迁移
- **L4 异步抽取** —— 带证据的记忆、语汇候选、幂等任务、taboo 隔离
- **L0 情绪/风险标志位** —— 隐晦危机表达可触发、谐谑不误判、危机资源离线常量
- **评估集与跑批** —— 20 条用例、确定性 + 模型裁判两套判据、硬门禁、基线对比
- **网页前端 + 生命周期** —— stdlib SSE 流式、页面关掉就自动停

`python -m pytest` → **258 项全绿**，单测不联网。

> 规格文档在父目录，**故意不进本仓库**：它是与评审方共享的单一事实源，拷进来就会分叉。

## 快速开始

```powershell
cd companion
python -m pytest -p no:cacheprovider   # 258 项，不联网（不落 .pytest_cache）
python tools/live_smoke.py            # 真实 API 两轮对话（需要 .env）
python tools/live_smoke.py --cache    # 附带 prompt cache 门槛扫描
python eval/run_eval.py               # 评估集（含模型裁判，约 10 次调用）
python eval/run_eval.py --no-judge     # 只跑确定性槽位，不花 token
python web/server.py --port 8765 --open
```

`.env`（已 gitignore）：

```ini
COMPANION_API_KEY=sk-...
COMPANION_BASE_URL=https://api.deepseek.com
COMPANION_MODEL=deepseek-flash
```

## 服务什么时候停

页面关了不该留一个空跑的进程。三个条件任一成立就退出（退出码 0）：

| 条件 | 触发 | 默认 |
|---|---|---|
| **关页面** | 最后一个页面 `pagehide` 时 `sendBeacon('/api/bye')` | 1.5s 后停（刷新会撤销） |
| **idle** | 页面每 20s 发一次 `/api/ping`，断了 | 90s |
| **max** | 硬上限，防锁屏没关浏览器 | 12 小时 |
| never-opened | 启动后一直没人访问 | 120s |

页面在加载时向 `/api/hello` 领一个 `page_id`，`ping` / `bye` 都带上它：
**关掉非最后一个标签页只注销，最后一个关掉才停服务**。实测关页面后约
2.9 秒端口停止服务（1.5s 宽限 + 关监听开销）。

```powershell
python web/server.py --idle-timeout 300        # 想长时间挂着看日志
python web/server.py --no-lifecycle            # 只能 Ctrl+C 结束
```

规则在 `web/lifecycle.py` 的 `decide_stop()`，是纯函数，单测覆盖 7 种组合；
真实进程也验证过三种原因：`never-opened` / `bye` / `idle`。

## 人格

默认是「温柔知心姐姐」（`src/companion/persona.py`），三块内容：

| 块 | 作用 |
|---|---|
| 身份与位置 | 年长几岁、稳、不评判；**没有具体生活细节**（无职业/家人/宠物/住处） |
| 被问到自己 | 一句轻的带过，把话头还回去；不编过去，被追问就承认说不清 |
| 口吻 | **开头不套同一招**：直接说事 / 接半句 / 短句 / 反问 / 留白；语气词放句尾或句中；禁用命令句开头 |
| **禁止的 AI 腔** | 不要「共情一句 + 问一句」二段式、不要「不是…而是…」、不要三连排比、不要复述、不要每句「听起来…」 |
| 回复形态 | 6 种混用：只应一声 / 接住就停 / 接住 + 一问 / 自我披露 / 轻调侃 / 替对方说完 |
| 量化参照 | 长度 10–50 字、问句至多一半、没要建议时 0 条、**自我披露 0 处**… |
| 方法 | 先接住再处理、情绪命名（不是每轮）、认可感受不评判做法、说「没事」时留门、不做诊断 |
| 正反对照 | 「别想太多，会好的」→「嗯——这话我听着都烦。」等 5 组 |
| 示范 | 16 条，**一半不带问号**，最短只有两个字 |

参考：[お姉さんのセリフ集](https://www.haruelf.com/entry/oneesan-serifu)、
Wikipedia [Signs of AI writing](https://en.wikipedia.org/wiki/Wikipedia:Signs_of_AI_writing)。

换人格只需在 `PERSONAS` 里加一个 `Persona`，不用改拼装逻辑。
测试守住两条边界：**不得自称心理医生/咨询师**，且人格必须装得进稳定前缀预算。

人格约 1270 token，稳定前缀因此到 ~1300——跨过实测的缓存门槛（1200 token 时二次命中 88%），
这也是为什么人格值得放在前缀里、而不是每轮动态生成。

## 网页版

```powershell
python web/server.py --port 8765 --data data --open
```

**历史对话按"天"组织**：一天就是一段对话，不再往下切分 session。

左侧每天一行（今天 / 昨天 / 日期+周X），带轮数、最后一句预览、起止时间。

- 点某一天 → 只看那天的对话
- 勾选多天 → 「查看选中」把几天的记录合并成一条时间线（按天加分隔）
- 删除：
  - 行尾 `✕` 删当天；`删除选中` 删勾选的多天
  - 气泡上的 `✕` 删单条
  - 顶栏「选择消息」进入多选模式，点选同一屏里的任意几条，底部操作条可
    全选本页 / 删除选中 / 取消
- 只有「查看今天」时输入框可用；看历史时禁用，避免把话写进过去

| 接口 | 作用 |
|---|---|
| `GET /api/days` | 按本地自然日聚合：轮数、条数、预览、首末时间 |
| `GET /api/history?days=a,b` | 合并指定日期的消息（本地时间） |
| `POST /api/delete` | `{days: [...]}` 或 `{message_ids: [...]}` |

删除会登记 `deletion_target`、递增 `data_revision`、清理 FTS，删完 `verify_fts()` 仍然干净。

只监听 `127.0.0.1`，不对外暴露。页面是单文件（`web/index.html`），回复走 SSE 流式；
`/api/health` 会如实报当前是 `sqlcipher` 还是 `plaintext-dev`。

## 加密（已落地）

`db.connect(path, key_hex=...)` 走 SQLCipher（实测 4.19.0 community）：

- 加密文件里**没有明文、没有 `SQLite format 3` 头、没有 key 打不开**；
- 没有 `sqlcipher3` 绑定时**直接抛错，不静默降级为明文**；
- 密钥 32 字节随机值，落盘前用 **Windows DPAPI** 保护（绑当前用户）；
- 不传 `key_hex` 时是明文库，`security_mode()` 会如实报 `plaintext-dev`。

```powershell
pip install sqlcipher3
python tools/encrypt_db.py "%LOCALAPPDATA%\shiye\data\companion.db"      # 明文 → 加密
python tools/encrypt_db.py "%LOCALAPPDATA%\shiye\data\companion.db" --verify-only
```

```python
conn = db.connect_secure("...\shiye\data\companion.db")  # 自动建密钥 + DPAPI 保护
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

## 文件放在哪

**仓库里只有源码。** 数据库、密钥、`.env` 都在用户数据目录：

```
%LOCALAPPDATA%\shiye\
  .env                  API key
  data\companion.db     对话数据库（SQLCipher 加密）
  data\companion.key    密钥（DPAPI 保护）
  pycache\              字节码缓存（避免 __pycache__ 落到源码里）
```

想换位置：设环境变量 `SHIYE_HOME` 即可（启动器会读它）。

```powershell
$env:SHIYE_HOME = 'D:\somewhere\shiye'
python web/server.py --open
```

重置一切：删掉 `%LOCALAPPDATA%\shiye` 整个目录，下次启动会重建。

## 目录

```
schema/001_init.sql     全部 DDL
schema/002_profile.sql  档案槽位（birthday / calendar / name / timezone）
schema/003_fts_sync.sql 脏标记触发器：绕过 wrapper 会被查出来
schema/004_session.sql  messages.session_id（会话隔离）
src/companion/
  db.py                 连接 / PRAGMA / 迁移 / FTS 同步与校验 / 文本检索
  crypto.py             DPAPI 密钥、口令备份、跨机恢复
  fts.py                CJK 分词（索引侧 unigram+bigram，查询侧 bigram）
  datewin.py            区间相交 与 跨年周年窗口
  timequery.py          双时间轴 + 三档置信 + 歧义保留
  recall.py             三路召回与配额融合（含 alias 归一）
  lexicon.py            语汇命中 / 查询扩展 / 注入记账
  deletion.py           事件簇 / 三态删除 / 级联 / data_revision
  signals.py            L0 情绪与风险标志位
  prompt.py             稳定前缀 + 动态段 + 注入防护 + 本地时间
  provider.py           OpenAI 兼容流式 provider（+ FakeProvider）
  turn.py               一轮对话的编排
  extract.py            L4 异步抽取（证据契约 / 幂等）
web/
  server.py             stdlib HTTP + SSE
  lifecycle.py          什么时候停
  index.html            单文件页面
tools/                  live_smoke / check_fts / encrypt_db
eval/run_eval.py        评估集与跑批
```

## 相关度过滤

检索不做"绝对分数下限"，而是**相对最强命中的比例**（`KEEP_RATIO_*`，默认 0.5）：
低于最强候选一半的条目直接丢弃。

早先用的是绝对阈值（τ=2.5），在 34 篇的标定集上精度 0.88 / 召回 1.00，
但换到只有几篇文档的评估集上把召回砍掉一半——**BM25 的分数依赖语料规模**，
绝对阈值等于把"语料有多大"焊进业务逻辑。

标定方法：

```powershell
python tools/calibrate_tau.py                    # 在 eval/tau_fixture.json 上扫比例
python -m pytest tests/test_recall.py            # 比例常数必须仍然达标
```

`tests/test_recall.py` 会验证这两个常数在标定集上精度/召回均 ≥0.8，改分词器
或改 BM25 用法时会先红。

> 诚实记录：这份标定集只能区分出一条噪声，比例本身没有被良好识别。
> 语料涨到几百条后应重新标注再跑一次。

## 已知取舍

- FTS 存的是分词结果，没有触发器分词，**增删改必须走 `db.upsert_*_fts`**。
  绕过去不会静默：`fts_dirty` 会留下记录，`tools/check_fts.py` 能查出来。
- 单字查询靠索引侧 unigram 兜住，索引体积约 2 倍。
- 长期记忆是全局的（单用户本就如此）；`session_id` 只隔离**原始对话窗口**。
- 时间一律 UTC 存储、本地展示。直接挂 UTC 时间戳会让模型把"晚上"说成"中午"。























