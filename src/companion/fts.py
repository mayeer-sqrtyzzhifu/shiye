"""CJK bigram tokenizer for SQLite FTS5.

SQLite 的 unicode61 分词器不切分中文：一整段 CJK 会变成一个 token，
因此 ``MATCH '生日'`` 永远匹配不到「去年生日那天」。把入库文本与查询
**同时**切成重叠 bigram 可以解决这个问题。

单字查询的坑
------------
纯 bigram 索引里，「猫」不在任何 bigram 中（unigram/bigram 都不含单字），
所以 ``MATCH '猫'`` 零命中。因此本模块采用**非对称**策略：

* 索引侧（``for_query=False``）：unigram **和** bigram 都存 —— 保证单字可召回；
* 查询侧（``for_query=True``）：只出 bigram；单字段落才出 unigram —— 保证精度。

代价是索引体积约 2 倍（个人规模可忽略），换来单字词不丢召回。

契约
----
* ``match_expr(query)`` 与写入侧必须用同一套 ``_TOKEN_RE``，否则召回率为 0。
* FTS5 的 ``bm25()`` 返回**负值**，越负越相关；排序必须写成
  ``ORDER BY -bm25(...) DESC``，或先转成 ``relevance = -bm25(...)``。
"""

from __future__ import annotations

import re

# CJK（含扩展 A、兼容区、假名）+ 拉丁数字词
_TOKEN_RE = re.compile(
    r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff]+"
    r"|[A-Za-z0-9_]+"
)


def tokenize(text: str | None, *, for_query: bool = False) -> list[str]:
    """Return CJK bigrams (+ unigrams on the index side) and lowercased words."""
    if not text:
        return []
    out: list[str] = []
    for match in _TOKEN_RE.finditer(text):
        seg = match.group(0)
        if seg[0].isascii():
            out.append(seg.lower())
        elif len(seg) == 1:
            out.append(seg)
        else:
            out.extend(seg[i : i + 2] for i in range(len(seg) - 1))
            if not for_query:
                out.extend(seg)  # 单字进索引，保证"猫"这类查询可召回
    return out


def tokenized_text(text: str | None) -> str:
    """Space-joined tokens, the form actually stored in the FTS column."""
    return " ".join(tokenize(text))


def match_expr(query: str | None) -> str | None:
    """Build an FTS5 MATCH expression (OR of quoted tokens), or None if empty."""
    tokens = list(dict.fromkeys(tokenize(query, for_query=True)))
    if not tokens:
        return None
    return " OR ".join(f'"{t}"' for t in tokens)
