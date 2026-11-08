"""OpenAI-compatible client for Colibri."""

import json
import urllib.request
from typing import Generator


class ColibriClient:
    def __init__(self, base_url: str, api_key: str, model: str,
                 max_tokens: int | None = None,
                 cache_slot: int = 0, temperature: float = 0.7,
                 enable_thinking: bool = False):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.max_tokens = max_tokens
        self.cache_slot = cache_slot
        self.temperature = temperature
        self.enable_thinking = enable_thinking

    def check_connection(self) -> tuple[bool, str]:
        """Ping Colibri and return (ok, message)."""
        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        url = f"{self.base_url}/models"
        try:
            req = urllib.request.Request(url, headers=headers, method="GET")
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.load(resp)
                models = [m.get("id", "?") for m in data.get("data", [])]
                if models:
                    return True, f"OK ({', '.join(models[:3])})"
                return True, "OK"
        except Exception as exc:
            return False, str(exc)

    def chat_stream(self, messages, tools=None, timeout=120) -> Generator[dict, None, None]:
        """Stream chat completions from Colibri.

        Sends browser-compatible parameters: cache_slot, context_compaction,
        enable_thinking, stream_options. Tools are optional (omitted when None).
        """
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "enable_thinking": self.enable_thinking,
            "cache_slot": self.cache_slot,
            "context_compaction": "auto",
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if self.max_tokens is not None:
            payload["max_tokens"] = self.max_tokens
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers, method="POST",
        )
        response = urllib.request.urlopen(req, timeout=timeout)
        try:
            for raw in response:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                chunk = json.loads(data)
                for choice in chunk.get("choices", []):
                    delta = choice.get("delta", {})
                    if delta.get("content"):
                        yield {"type": "text", "content": delta["content"]}
                    for part in delta.get("tool_calls") or []:
                        yield {
                            "type": "tool_call",
                            "index": part.get("index", 0),
                            "id": part.get("id", ""),
                            "name": (part.get("function") or {}).get("name", ""),
                            "arguments": (part.get("function") or {}).get("arguments", ""),
                        }
            yield {"type": "done"}
        finally:
            try:
                response.close()
            except Exception:
                pass