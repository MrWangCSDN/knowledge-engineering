"""DashScope（通义千问）LLM provider 适配器。

实现 LLMProviderProto.complete(system, user) 接口，调用通义 OpenAI 兼容端点。

环境变量：
  DASHSCOPE_API_KEY    必填，从阿里云 DashScope 控制台拿
  DASHSCOPE_MODEL      可选，默认 qwen-turbo（便宜）；可选 qwen-plus / qwen-max

参考文档：
  https://help.aliyun.com/zh/dashscope/developer-reference/compatibility-of-openai-with-dashscope
"""
from __future__ import annotations

import os
from typing import Any

import httpx


class DashScopeProvider:
    """通义千问 OpenAI 兼容接口的最简 client。

    用法：
        provider = DashScopeProvider()
        answer = await provider.complete(system="...", user="...")
    """

    BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    DEFAULT_MODEL = "qwen-turbo"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float = 60.0,
    ):
        self.api_key = api_key or os.getenv("DASHSCOPE_API_KEY", "")
        self.model = model or os.getenv("DASHSCOPE_MODEL", self.DEFAULT_MODEL)
        self.timeout = timeout
        if not self.api_key:
            raise RuntimeError(
                "DASHSCOPE_API_KEY 未设置；请在 .env.local 配置或通过 export 注入"
            )

    async def complete(self, *, system: str, user: str, **kwargs: Any) -> str:
        """同步式调用 LLM，返回完整回复字符串。

        v1 不做流式（synthesizer 也是同步式取 raw 输出再解析）。
        """
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.post(
                f"{self.BASE_URL}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    # 适度温度让回答有"业务说明"的语气，又不至于胡编
                    "temperature": 0.3,
                },
            )
            r.raise_for_status()
            data = r.json()
            return data["choices"][0]["message"]["content"]
