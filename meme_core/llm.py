"""独立 OpenAI 兼容视觉客户端，用于 AI 打标。

接口对齐 tagging.TaggingService 期望的 provider：text_chat(...) 返回带 completion_text 的对象。
本地图片在发送前转成 base64 data URL。
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path

import aiohttp

_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}


@dataclass
class LLMResponse:
    completion_text: str


def _to_data_url(path: str) -> str:
    p = Path(path)
    mime = _MIME.get(p.suffix.lower(), "image/png")
    raw = p.read_bytes()
    encoded = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{encoded}"


class VisionClient:
    def __init__(self, base_url: str, api_key: str, model: str, timeout_s: int = 60):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout_s = timeout_s

    @property
    def ready(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)

    async def text_chat(self, prompt: str, system_prompt: str = "",
                        context=None, image_urls=None, model: str = ""):
        if not self.ready:
            raise RuntimeError("视觉客户端未配置完整（base_url/api_key/model）")
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        content = [{"type": "text", "text": prompt}]
        for img in image_urls or []:
            url = img if img.startswith("http") else _to_data_url(img)
            content.append({"type": "image_url", "image_url": {"url": url}})
        messages.append({"role": "user", "content": content})

        payload = {
            "model": model or self.model,
            "messages": messages,
            "stream": False,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        timeout = aiohttp.ClientTimeout(total=self.timeout_s)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                f"{self.base_url}/chat/completions", json=payload, headers=headers
            ) as resp:
                body = await resp.json()
                if resp.status != 200:
                    raise RuntimeError(f"视觉调用失败 HTTP {resp.status}: {str(body)[:200]}")
                text = body["choices"][0]["message"].get("content") or ""
        return LLMResponse(completion_text=text)
