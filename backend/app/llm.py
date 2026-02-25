from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass
class LLMResponse:
    text: str
    raw: dict[str, Any] | None = None


class LLMClient:
    def __init__(self, base_url: str, api_key: str, temperature: float = 0.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.temperature = temperature

    @property
    def enabled(self) -> bool:
        return bool(self.api_key and self.base_url)

    async def chat(
        self,
        model: str,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = 1200,
        temperature: float | None = None,
    ) -> LLMResponse | None:
        if not self.enabled:
            return None

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": self.temperature if temperature is None else temperature,
            "max_tokens": max_tokens,
        }

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=80.0) as client:
                response = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers=headers,
                    json=payload,
                )
            response.raise_for_status()
            data = response.json()
            choices = data.get("choices") or []
            if not choices:
                return None
            message = choices[0].get("message") or {}
            content = message.get("content", "")
            if isinstance(content, list):
                text_parts = []
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        text_parts.append(str(part.get("text", "")))
                    elif isinstance(part, str):
                        text_parts.append(part)
                text = "\n".join(text_parts).strip()
            else:
                text = str(content).strip()
            if not text:
                return None
            return LLMResponse(text=text, raw=data)
        except Exception:
            return None


def parse_json_object(text: str) -> dict[str, Any] | None:
    """Try to parse a JSON object from mixed model output."""

    if not text.strip():
        return None

    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        return None

    candidate = match.group(0)
    try:
        data = json.loads(candidate)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        return None

    return None
