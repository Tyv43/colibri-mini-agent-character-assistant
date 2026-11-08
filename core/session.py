"""Session persistence: save/load conversation history."""

import json
import os
import uuid
from pathlib import Path


class Session:
    def __init__(self, system_prompt: str, file_path: Path | None = None,
                 max_messages: int = 40, max_chars: int = 40_000):
        self.file_path = file_path
        self.max_messages = max_messages
        self.max_chars = max_chars
        self.history: list[dict] = [{"role": "system", "content": system_prompt}]
        self.session_id = uuid.uuid4().hex[:12]
        self._load()

    def _load(self):
        """Load session from file if it exists."""
        if not self.file_path or not self.file_path.exists():
            return
        data = json.loads(self.file_path.read_text(encoding="utf-8"))
        messages = data.get("messages")
        if not isinstance(messages, list):
            return
        sid = data.get("session_id")
        if isinstance(sid, str) and sid:
            self.session_id = sid
        self.history = messages
        if not self.history or self.history[0].get("role") != "system":
            self.history.insert(0, {"role": "system", "content": ""})
        self.trim()

    def save(self):
        """Save session to file."""
        if not self.file_path:
            return
        self.trim()
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"session_id": self.session_id, "messages": self.history}
        tmp = self.file_path.with_name(self.file_path.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.file_path)

    def trim(self):
        """Trim history to fit within max_messages and max_chars."""
        # Trim by max_messages — remove oldest messages from the beginning
        while len(self.history) > self.max_messages and len(self.history) > 2:
            del self.history[1]
        # Trim by max_chars
        while len(self.history) > 2 and sum(len(json.dumps(m, ensure_ascii=False)) for m in self.history) > self.max_chars:
            if len(self.history) > 3:
                del self.history[1]
            else:
                break

    def reset(self, system_prompt: str):
        """Reset session with a new system prompt."""
        self.history = [{"role": "system", "content": system_prompt}]
        self.session_id = uuid.uuid4().hex[:12]
        self.save()

    def restore(self, new_history: list[dict]):
        """Replace history with compacted version (after context rotation).

        Does NOT generate a new session_id — the conversation continues
        the same logical session, just with compressed context.
        """
        self.history = new_history
        self.save()