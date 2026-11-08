"""Context manager: monitor context usage and rotate session when threshold reached.

Works inline with the agent loop — no separate daemon needed.
Called between turns when agent is idle.
"""

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path


SUMMARY_PROMPT = """Your context is nearing its limit. Create a compact handoff summary of the entire conversation so far.
Do not continue the task, do not answer the last user message, and do not run commands.
Use the main language of the conversation. Output plain text with these sections:
1. GOAL — the user's current objective.
2. DURABLE FACTS — important preferences, constraints, environment details, exact paths, and decisions that must survive compaction.
3. WORK DONE — completed actions and important command results.
4. CURRENT STATE — what is happening now and what remains unfinished.
5. RECENT CONVERSATION — the latest messages in more detail, preserving the current thread of thought.
Be concise about old messages and more detailed about recent ones. Preserve concrete facts exactly; do not invent missing information."""

RESTORE_PREFIX = "[CONTEXT RESTORE]\nThis is a service restoration of a previous conversation, not a new task.\nUse the summary as already known context and continue the conversation naturally.\n\n"
RESTORE_SUFFIX = "\n[/CONTEXT RESTORE]"


class ContextManager:
    """Monitor context usage and rotate session when threshold reached."""

    def __init__(
        self,
        colibri,
        session,
        config_dir: Path,
        threshold: float = 75.0,
        summary_max_tokens: int = 1200,
        recent_keep: int = 4,
        archive_dir: str = "sessions",
        log_fn=None,
    ):
        self.colibri = colibri
        self.session = session
        self.config_dir = Path(config_dir)
        self.threshold = threshold
        self.summary_max_tokens = summary_max_tokens
        self.recent_keep = recent_keep
        self.log_fn = log_fn or (lambda msg: None)

        # Archive directory
        self.archive_path = self.config_dir / archive_dir
        self.archive_path.mkdir(parents=True, exist_ok=True)

        # Persona (set via set_persona)
        self._persona = {}

        # State
        self._last_context_tokens = 0
        self._last_usage_percent = 0.0
        self._last_compaction = 0
        self._compaction_count = 0
        self._rotating = False  # guard against double rotation

    def set_persona(self, persona: dict, persona_dir: Path | None = None):
        """Bind persona data for context restore block generation."""
        self._persona = persona
        self._persona_dir = persona_dir

    def set_mode(self, mode: str):
        """Set agent mode for system prompt generation during rotation."""
        self._mode = mode

    def set_technical_prompt(self, prompt: str):
        """Set technical instructions for system prompt during rotation."""
        self._technical_prompt = prompt

    # ── Context measurement ────────────────────────────────────

    def _server_url(self) -> str:
        """Get base server URL (without /v1)."""
        url = self.colibri.base_url.rstrip("/")
        if url.endswith("/v1"):
            return url[:-3]
        return url

    def _get_json(self, url: str) -> dict:
        headers = {}
        if self.colibri.api_key:
            headers["Authorization"] = f"Bearer {self.colibri.api_key}"
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.load(resp)

    def read_context(self) -> dict:
        """Read current context usage from Colibri API.

        Tries /health + /profile (Colibri-specific). Falls back to
        character-based estimation if endpoints are unavailable.
        """
        server = self._server_url()

        # Try Colibri /health endpoint
        try:
            health = self._get_json(f"{server}/health")
            context_length = int(health.get("context_tokens", 0))
        except Exception:
            # Fallback: estimate from history size
            return self._estimate_context()

        if not context_length:
            return self._estimate_context()

        live = health.get("live") or []
        if live:
            current = live[0]
            prompt_tokens = int(current.get("prompt_tokens") or 0)
            generated_tokens = int(current.get("tokens") or 0)
            total = prompt_tokens + generated_tokens
            self._last_context_tokens = total
            self._last_usage_percent = total * 100 / context_length if context_length else 0
            return {
                "context_length": context_length,
                "total_tokens": total,
                "usage_percent": self._last_usage_percent,
                "active": True,
                "source": f"/health.live ({current.get('state', 'active')})",
            }

        # /profile — last completed turn
        try:
            profile = self._get_json(f"{server}/profile")
        except Exception:
            return self._estimate_context(context_length)

        turns = profile.get("turns") or []
        if not turns:
            return {
                "context_length": context_length,
                "total_tokens": 0,
                "usage_percent": 0.0,
                "active": False,
                "source": "/profile (no turns yet)",
            }
        latest = turns[-1]
        prompt_tokens = int(latest.get("prompt_tokens") or 0)
        # prompt_tokens already includes full history + system prompt
        total = prompt_tokens
        self._last_context_tokens = total
        self._last_usage_percent = total * 100 / context_length if context_length else 0
        return {
            "context_length": context_length,
            "total_tokens": total,
            "usage_percent": self._last_usage_percent,
            "active": False,
            "source": "/profile (last completed turn)",
        }

    def _estimate_context(self, context_length: int = 65536) -> dict:
        """Estimate context usage from history size (fallback).

        Used when /health or /profile are unavailable (non-Colibri servers).
        Estimates ~4 characters per token.
        """
        history_chars = sum(len(json.dumps(m, ensure_ascii=False)) for m in self.session.history)
        estimated_tokens = history_chars // 4
        self._last_context_tokens = estimated_tokens
        self._last_usage_percent = estimated_tokens * 100 / context_length if context_length else 0
        return {
            "context_length": context_length,
            "total_tokens": estimated_tokens,
            "usage_percent": self._last_usage_percent,
            "active": False,
            "source": "estimated (~4 chars/token)",
        }

    def needs_compaction(self) -> bool:
        """Check if context usage exceeds threshold."""
        try:
            ctx = self.read_context()
        except Exception as exc:
            self.log_fn(f"context check failed: {exc}")
            return False

        if ctx["active"]:
            self.log_fn(f"context {ctx['usage_percent']:.1f}% (active generation, skip)")
            return False

        if ctx["total_tokens"] == 0:
            return False

        threshold_tokens = int(ctx["context_length"] * self.threshold / 100)
        if ctx["total_tokens"] < threshold_tokens:
            return False

        self.log_fn(
            f"context {ctx['total_tokens']}/{ctx['context_length']} "
            f"({ctx['usage_percent']:.1f}%) ≥ {self.threshold}% — compaction needed"
        )
        return True

    # ── Summary generation ─────────────────────────────────────

    def _generate_summary(self, messages: list[dict]) -> tuple[str, dict]:
        """Send summary request to Colibri and return (summary_text, usage).

        Uses cache_slot=0 (same as main session) since the server likely
        has only one slot. The session is being replaced anyway.
        """
        summary_messages = list(messages) + [{"role": "user", "content": SUMMARY_PROMPT}]

        payload = {
            "model": self.colibri.model,
            "messages": summary_messages,
            "temperature": 0.2,
            "enable_thinking": False,
            "cache_slot": 0,
            "context_compaction": "auto",
            "stream": True,
            "stream_options": {"include_usage": True},
            "max_tokens": self.summary_max_tokens,
        }
        headers = {"Content-Type": "application/json"}
        if self.colibri.api_key:
            headers["Authorization"] = f"Bearer {self.colibri.api_key}"

        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            f"{self.colibri.base_url}/chat/completions",
            data=data,
            headers=headers,
            method="POST",
        )

        text_parts: list[str] = []
        usage: dict = {}

        try:
            with urllib.request.urlopen(req, timeout=180) as response:
                for raw in response:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    chunk_data = line[5:].strip()
                    if chunk_data == "[DONE]":
                        break
                    chunk = json.loads(chunk_data)
                    if chunk.get("usage") is not None:
                        usage = chunk["usage"]
                    for choice in chunk.get("choices", []):
                        content = choice.get("delta", {}).get("content")
                        if content:
                            text_parts.append(content)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:200]
            raise RuntimeError(f"summary generation failed: HTTP {exc.code}: {body}") from exc
        except Exception as exc:
            raise RuntimeError(f"summary generation failed: {exc}") from exc

        return "".join(text_parts).strip(), usage

    # ── Session rotation ────────────────────────────────────────

    def rotate(self) -> dict:
        """Perform full context rotation with error handling.

        Returns dict with: success, before_tokens, after_tokens,
        summary_tokens, archive_path, error.
        """
        # Guard against double rotation
        if self._rotating:
            return {"success": False, "error": "rotation already in progress"}

        self._rotating = True
        result = {
            "success": False,
            "before_tokens": 0,
            "after_tokens": 0,
            "summary_tokens": 0,
            "archive_path": "",
            "error": "",
        }

        try:
            # Read context
            try:
                ctx = self.read_context()
                result["before_tokens"] = ctx["total_tokens"]
            except Exception as exc:
                result["error"] = f"context read failed: {exc}"
                self.log_fn(result["error"])
                return result

            # 1. Archive current session
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            archive_name = f"arch_{timestamp}.json"
            archive_file = self.archive_path / archive_name

            if self.session.file_path and self.session.file_path.exists():
                try:
                    archive_file.write_text(
                        self.session.file_path.read_text(encoding="utf-8"),
                        encoding="utf-8",
                    )
                    result["archive_path"] = str(archive_file)
                    self.log_fn(f"archived session → {archive_name}")
                except Exception as exc:
                    self.log_fn(f"archive failed (non-critical): {exc}")

            # 2. Generate summary (with retry — max 3 attempts)
            messages = list(self.session.history)
            if not messages or len(messages) < 2:
                result["error"] = "session too small to compact"
                self.log_fn(result["error"])
                return result

            summary = None
            usage = {}
            max_summary_retries = 3
            for attempt in range(1, max_summary_retries + 1):
                try:
                    summary, usage = self._generate_summary(messages)
                    if summary and len(summary.strip()) >= 50:
                        break
                    self.log_fn(f"summary attempt {attempt}/{max_summary_retries}: too short, retrying")
                except Exception as exc:
                    self.log_fn(f"summary attempt {attempt}/{max_summary_retries} failed: {exc}")
                    summary = None
                    if attempt == max_summary_retries:
                        result["error"] = f"summary generation failed after {max_summary_retries} attempts: {exc}"
                        self.log_fn(result["error"])
                        return result
                    time.sleep(1)  # brief pause before retry

            if not summary or len(summary.strip()) < 50:
                result["error"] = "summary too short or empty — aborting rotation"
                self.log_fn(result["error"])
                return result

            summary_tokens = int(usage.get("completion_tokens", 0))
            result["summary_tokens"] = summary_tokens

            # Save summary
            try:
                summary_file = self.archive_path / f"summary_{timestamp}.md"
                summary_file.write_text(
                    f"# Context Summary — {timestamp}\n"
                    f"Before: {result['before_tokens']} tokens\n"
                    f"Summary: {summary_tokens} tokens\n\n"
                    f"{summary}\n",
                    encoding="utf-8",
                )
                self.log_fn(f"summary saved → {summary_file.name} ({summary_tokens} tokens)")
            except Exception as exc:
                self.log_fn(f"summary save failed (non-critical): {exc}")

            # 3. Build new session history
            system_msg = messages[0] if messages and messages[0].get("role") == "system" else {
                "role": "system", "content": "",
            }

            if self._persona:
                from core.persona import build_context_restore, build_system_prompt
                context_restore = build_context_restore(self._persona, summary)
                mode = getattr(self, '_mode', 'rp_agent')
                technical = getattr(self, '_technical_prompt', '')
                pdir = getattr(self, '_persona_dir', None)
                new_system_content = build_system_prompt(
                    self._persona, context_block=context_restore,
                    mode=mode, technical_prompt=technical, persona_dir=pdir,
                )
            else:
                context_restore = RESTORE_PREFIX + summary + RESTORE_SUFFIX
                new_system_content = system_msg.get("content", "") + "\n\n" + context_restore

            # Keep last N messages verbatim (exclude system messages)
            non_system = [m for m in messages if m.get("role") != "system"]
            recent = non_system[-self.recent_keep:] if len(non_system) > self.recent_keep else non_system

            new_history = [
                {"role": "system", "content": new_system_content},
                {"role": "assistant", "content": "Understood. Context restored. Continuing."},
            ]
            new_history.extend(recent)

            # 4. Atomically replace session file
            old_session_path = self.session.file_path
            if old_session_path:
                try:
                    payload = {
                        "session_id": self.session.session_id,
                        "messages": new_history,
                    }
                    tmp = old_session_path.with_name(old_session_path.name + ".tmp")
                    tmp.write_text(
                        json.dumps(payload, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    os.chmod(tmp, 0o600)
                    os.replace(tmp, old_session_path)
                except Exception as exc:
                    result["error"] = f"session file replacement failed: {exc}"
                    self.log_fn(result["error"])
                    return result

            # 5. Update in-memory session
            self.session.history = new_history
            try:
                self.session.save()
            except Exception as exc:
                self.log_fn(f"session save after rotation failed (non-critical): {exc}")

            self._compaction_count += 1
            self._last_compaction = time.time()
            result["success"] = True

            # 6. Estimate new context size
            try:
                new_system_chars = len(new_system_content)
                recent_chars = sum(len(json.dumps(m, ensure_ascii=False)) for m in recent)
                estimated_tokens = (new_system_chars + recent_chars) // 4
                result["after_tokens"] = estimated_tokens
                self.log_fn(
                    f"rotation complete: {result['before_tokens']} → "
                    f"~{estimated_tokens} tokens (summary: {summary_tokens})"
                )
            except Exception:
                self.log_fn("rotation complete (measurement skipped)")

            return result

        except Exception as exc:
            result["error"] = f"unexpected error during rotation: {exc}"
            self.log_fn(result["error"])
            return result
        finally:
            self._rotating = False

    # ── Status ──────────────────────────────────────────────────

    def status(self) -> dict:
        """Return current context status for display."""
        return {
            "usage_percent": round(self._last_usage_percent, 1),
            "total_tokens": self._last_context_tokens,
            "threshold": self.threshold,
            "compaction_count": self._compaction_count,
            "last_compaction": self._last_compaction,
        }