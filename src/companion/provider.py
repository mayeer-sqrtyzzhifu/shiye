"""模型 provider（§7 的"网络"那一段）。

只实现一个真实适配器：**OpenAI 兼容的 chat/completions**（DeepSeek 走这条）。
刻意不引 SDK：少一层依赖、流式与 usage 字段都看得见。

三条与规格直接相关的设计：

1. **reasoning 与 content 分开。** 实测该端点默认输出 ``reasoning_content``，
   且 reasoning 会计入 ``completion_tokens``。预算里必须把它算进去，否则
   正文会被推理吃掉（第一次探测就是这样：16 个 token 全是 reasoning，
   ``content`` 为空）。
2. **usage 里带 cache 命中数。** ``prompt_cache_hit_tokens`` /
   ``prompt_cache_miss_tokens`` 能直接验证 §6.3 的前缀缓存假设，不用猜。
3. **key 只从环境或 .env 读**，不写进代码、不落日志。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Protocol

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore

from . import paths  # noqa: E402

ENV_FILE = paths.env_file()


def load_env(path: Path | None = None) -> dict[str, str]:
    """极简 .env 读取；已存在的环境变量优先（便于 CI / 临时覆盖）。

    默认依次读用户数据目录的 .env、再读仓库根目录的旧 .env（兼容早期布局）。
    """
    candidates = [path] if path is not None else [ENV_FILE, paths.legacy_env_file()]
    values: dict[str, str] = {}
    for candidate in candidates:
        if candidate is None or not candidate.exists():
            continue
        for line in candidate.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values.setdefault(key.strip(), value.strip())
    for key, value in values.items():
        os.environ.setdefault(key, value)
    return values


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass(frozen=True)
class Completion:
    text: str
    reasoning: str = ""
    usage: Usage = field(default_factory=Usage)
    finish_reason: str = "stop"
    model: str = ""


class Provider(Protocol):
    def stream(
        self,
        system: str,
        messages: list[dict[str, str]],
        *,
        on_text: Callable[[str], None] | None = None,
        max_tokens: int = 800,
        temperature: float = 0.8,
        timeout: float = 60.0,
    ) -> Completion: ...


class ProviderError(RuntimeError):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"provider returned {status}: {body[:300]}")
        self.status = status
        self.body = body


class OpenAICompatProvider:
    """OpenAI 兼容的流式 chat provider。"""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
    ) -> None:
        load_env()
        self.api_key = api_key or os.environ.get("COMPANION_API_KEY", "")
        self.base_url = (base_url or os.environ.get("COMPANION_BASE_URL")
                         or "https://api.deepseek.com").rstrip("/")
        self.model = model or os.environ.get("COMPANION_MODEL", "deepseek-flash")
        if not self.api_key:
            raise RuntimeError("缺少 COMPANION_API_KEY（环境变量或 .env）")

    @property
    def endpoint(self) -> str:
        return f"{self.base_url}/chat/completions"

    def stream(
        self,
        system: str,
        messages: list[dict[str, str]],
        *,
        on_text: Callable[[str], None] | None = None,
        max_tokens: int = 800,
        temperature: float = 0.8,
        timeout: float = 60.0,
    ) -> Completion:
        if httpx is None:  # pragma: no cover
            raise RuntimeError("需要 httpx：pip install httpx")

        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, *messages],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        text: list[str] = []
        reasoning: list[str] = []
        usage = Usage()
        finish = "stop"
        model = self.model

        with httpx.stream(
            "POST",
            self.endpoint,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=timeout,
        ) as response:
            if response.status_code != 200:
                raise ProviderError(response.status_code, response.read().decode("utf-8", "replace"))
            for line in response.iter_lines():
                if not line or not line.startswith("data: "):
                    continue
                data = line[6:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                model = chunk.get("model", model)
                if chunk.get("usage"):
                    usage = _parse_usage(chunk["usage"])
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    if delta.get("reasoning_content"):
                        reasoning.append(delta["reasoning_content"])
                    piece = delta.get("content")
                    if piece:
                        text.append(piece)
                        if on_text:
                            on_text(piece)
                    if choice.get("finish_reason"):
                        finish = choice["finish_reason"]

        return Completion(
            text="".join(text),
            reasoning="".join(reasoning),
            usage=usage,
            finish_reason=finish,
            model=model,
        )


def _parse_usage(raw: dict) -> Usage:
    details = raw.get("completion_tokens_details") or {}
    return Usage(
        prompt_tokens=int(raw.get("prompt_tokens") or 0),
        completion_tokens=int(raw.get("completion_tokens") or 0),
        reasoning_tokens=int(details.get("reasoning_tokens") or 0),
        cache_hit_tokens=int(raw.get("prompt_cache_hit_tokens") or 0),
        cache_miss_tokens=int(raw.get("prompt_cache_miss_tokens") or 0),
    )


class FakeProvider:
    """测试用：不联网，按脚本回话，并记录收到的消息。"""

    def __init__(self, replies: Iterable[str] = ("好的，我在。",)) -> None:
        self._replies = list(replies)
        self.calls: list[dict] = []

    def stream(
        self,
        system: str,
        messages: list[dict[str, str]],
        *,
        on_text: Callable[[str], None] | None = None,
        max_tokens: int = 800,
        temperature: float = 0.8,
        timeout: float = 60.0,
    ) -> Completion:
        self.calls.append({"system": system, "messages": messages})
        reply = self._replies[min(len(self.calls) - 1, len(self._replies) - 1)]
        if on_text:
            on_text(reply)
        return Completion(
            text=reply,
            usage=Usage(prompt_tokens=len(system) // 2, completion_tokens=len(reply) // 2),
            finish_reason="stop",
            model="fake",
        )

