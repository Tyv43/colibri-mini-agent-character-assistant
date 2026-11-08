"""Agent core: model → command → approve → execute loop.

IPC for tmux panels:
- .state.json — agent state (agent writes, commands panel reads)
- .cmd_fifo — approve commands (commands panel writes, agent reads)
- .agent.log — event log (tail -f)
"""

import json
import os
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from urllib.error import HTTPError

from core.colibri_client import ColibriClient
from core.context_manager import ContextManager
from core.permissions import needs_approval
from core.session import Session
from utils.commands import tail_text, terminate_process


class AgentState:
    IDLE = "idle"
    WAITING_MODEL = "waiting_model"
    WAITING_APPROVAL = "waiting_approval"
    RUNNING_COMMAND = "running_command"
    WAITING_DECISION = "waiting_decision"
    COMPACTING = "compacting"


class Agent:
    """Local agent with file-based IPC for tmux panels."""

    def __init__(
        self,
        colibri: ColibriClient,
        system_prompt: str,
        tools: list[dict],
        workspace: Path,
        session: Session,
        command_timeout: int = 60,
        max_steps: int = 3,
        permission_mode: str = "ask",
        auto_continue: bool = False,
        mode: str = "agent",
        approval_location: str = "chat",
    ):
        self.colibri = colibri
        self.system_prompt = system_prompt
        self.tools = tools
        self.workspace = workspace
        self.session = session
        self.command_timeout = command_timeout
        self.max_steps = max_steps
        self.permission_mode = permission_mode
        self.auto_continue = auto_continue
        self.mode = mode  # agent | rp_agent | chat
        self.approval_location = approval_location  # chat | cmd

        # IPC paths
        self.state_path = workspace / ".state.json"
        self.fifo_path = workspace / ".cmd_fifo"
        self.log_path = workspace / ".agent.log"

        # State
        self._lock = threading.RLock()
        self._state = AgentState.IDLE
        self._stop_event = threading.Event()
        self._current_process = None
        self._commands_run = 0
        self._commands_seen: set[str] = set()
        self._pending_command: str | None = None
        self._last_result: dict | None = None
        self._approval_result = False
        self._approval_event: threading.Event | None = None

        # Log file
        self._log_file = open(self.log_path, "a", encoding="utf-8", buffering=1)

        # Callbacks (for chat client)
        self.on_chat = lambda text, role="assistant": None
        self.on_chat_chunk = lambda text: None
        self.on_command = lambda command: None
        self.on_output = lambda result: None
        self.on_status = lambda state: None
        self.on_done = lambda: None
        self.on_compaction = lambda info: None

        # Context manager
        self._context_mgr = None  # initialized after construction

        # Create FIFO
        self._ensure_fifo()

        # Start FIFO reader thread
        self._fifo_thread = threading.Thread(target=self._fifo_reader, daemon=True)
        self._fifo_thread.start()

        # Save initial state
        self._save_state()

    def _ensure_fifo(self):
        """Create .cmd_fifo if it doesn't exist."""
        if not self.fifo_path.exists():
            os.mkfifo(self.fifo_path)

    def _fifo_reader(self):
        """Read approve commands from FIFO."""
        while True:
            try:
                fd = os.open(str(self.fifo_path), os.O_RDONLY)
                try:
                    while True:
                        data = os.read(fd, 4096)
                        if not data:
                            break
                        for line in data.decode("utf-8", errors="replace").splitlines():
                            line = line.strip()
                            if line == "approve":
                                self._approve_from_fifo(True)
                            elif line == "reject":
                                self._approve_from_fifo(False)
                            elif line == "continue":
                                self._continue_from_fifo()
                            elif line == "done":
                                self._done_from_fifo()
                            elif line == "stop":
                                self._stop_from_fifo()
                            elif line == "halt":
                                self._halt_from_fifo()
                            elif line.startswith("mode:"):
                                mode = line[5:].strip()
                                if mode in ("ask", "whitelist", "allow_except_rm", "allow_all"):
                                    self.set_mode(mode)
                            elif line.startswith("auto:"):
                                val = line[5:].strip()
                                if val in ("on", "off"):
                                    self.set_auto_continue(val == "on")
                finally:
                    os.close(fd)
            except Exception:
                time.sleep(0.1)

    def _approve_from_fifo(self, approved: bool):
        """Handle approve from FIFO (commands panel)."""
        with self._lock:
            if self._state != AgentState.WAITING_APPROVAL:
                self._log(f"approve ignored: state={self._state}")
                return
            self._approval_result = approved
            if self._approval_event:
                self._approval_event.set()

    def _continue_from_fifo(self):
        with self._lock:
            if self._state != AgentState.WAITING_DECISION:
                self._log(f"continue ignored: state={self._state}")
                return
            if self._commands_run >= self.max_steps:
                self._commands_run = 0
                self._commands_seen.clear()
                self._log("continue: reset counter after limit")
            self._last_result = None
            self._stop_event.clear()  # safe here — we're in WAITING_DECISION, not mid-stop
            self._set_state(AgentState.WAITING_MODEL)
        self._log("continue: starting model turn")
        threading.Thread(target=self._model_loop, daemon=True).start()

    def _done_from_fifo(self):
        with self._lock:
            if self._state != AgentState.WAITING_DECISION:
                return
            self._last_result = None
            self._set_state(AgentState.IDLE)
        self.on_done()
        self._log("done")
        self._check_context_rotation()

    def _stop_from_fifo(self):
        self.stop()

    def _halt_from_fifo(self):
        """Soft halt: kill the loop, tell model 'interrupted'."""
        with self._lock:
            proc = self._current_process
            self._current_process = None
            self._pending_command = None
        if proc:
            terminate_process(proc)
        # Set stop_event to interrupt current _model_loop
        self._stop_event.set()
        # Wait for _model_loop to notice stop_event (max 1.5s)
        for _ in range(30):
            if self._state == AgentState.IDLE:
                break
            time.sleep(0.05)
        # Clear stop_event for the new _model_loop
        self._stop_event.clear()
        with self._lock:
            if self._state != AgentState.IDLE:
                self._record_result("[Cycle interrupted by user. Don't run more commands. Answer based on what you have.]")
                self._set_state(AgentState.WAITING_MODEL)
            else:
                return
        self._log("halted by user")
        threading.Thread(target=self._model_loop, daemon=True).start()

    def _save_state(self):
        """Write .state.json for commands panel."""
        with self._lock:
            last_result = self._last_result
            if last_result and not last_result.get("error"):
                r = dict(last_result)
                if r.get("stdout"):
                    r["stdout"] = r["stdout"][:2000]
                if r.get("stderr"):
                    r["stderr"] = r["stderr"][:2000]
                last_result = r
            state = {
                "state": self._state,
                "pending_command": self._pending_command,
                "commands_run": self._commands_run,
                "permission_mode": self.permission_mode,
                "auto_continue": self.auto_continue,
                "last_result": last_result,
            }
        try:
            tmp = self.state_path.with_name(self.state_path.name + ".tmp")
            tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.state_path)
        except Exception:
            pass

    def _log(self, msg: str):
        """Write to log file."""
        try:
            ts = time.strftime("%H:%M:%S")
            self._log_file.write(f"[{ts}] {msg}\n")
            self._log_file.flush()
        except (ValueError, OSError):
            pass

    # ── Properties ───────────────────────────────────────────────

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def pending_command(self) -> str | None:
        with self._lock:
            return self._pending_command

    # ── Public methods ──────────────────────────────────────────

    def send_message(self, text: str):
        text = text.strip()
        if not text:
            return
        with self._lock:
            if self._state != AgentState.IDLE:
                raise RuntimeError(f"Agent busy ({self._state}). Use /stop or /reset.")
            # Clear stop_event from any previous stop()
            self._stop_event.clear()
            self._commands_run = 0
            self._commands_seen.clear()
            self._last_result = None
            self._pending_command = None
            if self.session.max_messages <= 0:
                self.session.history = [{"role": "system", "content": self.system_prompt}]
            self.session.history.append({"role": "user", "content": text})
            self.session.save()
            self._set_state(AgentState.WAITING_MODEL)
        self._log(f"message: {text}")
        self.on_chat(text, role="user")
        threading.Thread(target=self._model_loop, daemon=True).start()

    def approve(self, approved: bool):
        """Approve from chat client (not FIFO)."""
        with self._lock:
            if self._state != AgentState.WAITING_APPROVAL:
                raise RuntimeError("No command waiting for approval.")
            self._approval_result = approved
            if self._approval_event:
                self._approval_event.set()

    def continue_task(self):
        with self._lock:
            if self._state != AgentState.WAITING_DECISION:
                raise RuntimeError("No result to pass to model (not in waiting_decision).")
            if self._last_result is None:
                raise RuntimeError("No result available.")
            if self._commands_run >= self.max_steps:
                self._commands_run = 0
                self._commands_seen.clear()
                self._log("continue: reset counter after limit")
            self._last_result = None
            self._stop_event.clear()
            self._set_state(AgentState.WAITING_MODEL)
        self._log("continue: starting model turn")
        threading.Thread(target=self._model_loop, daemon=True).start()

    def done(self):
        with self._lock:
            if self._state != AgentState.WAITING_DECISION:
                raise RuntimeError("No completed command.")
            self._last_result = None
            self._set_state(AgentState.IDLE)
        self.on_done()
        self._log("done")
        self._check_context_rotation()

    def stop(self):
        self._stop_event.set()
        with self._lock:
            proc = self._current_process
            self._current_process = None
            self._pending_command = None
            self._last_result = None
            self._set_state(AgentState.IDLE)
        if proc:
            terminate_process(proc)
        self._log("stopped")
        # Don't clear _stop_event here — send_message() will clear it

    def reset(self):
        self.stop()
        with self._lock:
            self._commands_run = 0
            self._commands_seen.clear()
        self._log("reset")

    def new_session(self):
        self.stop()
        with self._lock:
            self.session.reset(self.system_prompt)
            self._commands_run = 0
            self._commands_seen.clear()
        self._log("new_session")

    def set_mode(self, mode: str):
        if mode not in ("ask", "whitelist", "allow_except_rm", "allow_all"):
            raise ValueError(f"Unknown mode: {mode}")
        with self._lock:
            if self._state not in (AgentState.IDLE, AgentState.WAITING_APPROVAL, AgentState.WAITING_DECISION):
                raise RuntimeError(f"Agent busy ({self._state}). Use /stop before changing mode.")
            self.permission_mode = mode
        self._log(f"mode: {mode}")
        self._save_state()

    def set_auto_continue(self, value: bool):
        self.auto_continue = value
        self._log(f"auto_continue: {value}")
        self._save_state()

    def start_greeting(self):
        """Send the system prompt as the opening message to trigger a response.

        Used in rp_agent and chat modes — model reacts to the system prompt
        and user gets a response showing the bot is ready.
        """
        with self._lock:
            if self._state != AgentState.IDLE:
                return
            self._stop_event.clear()
            self._commands_run = 0
            self._commands_seen.clear()
            self._last_result = None
            self._pending_command = None
            self.session.history.append({
                "role": "user",
                "content": "[Start the conversation. Greet the user in character. Respond in English.]",
            })
            self.session.save()
            self._set_state(AgentState.WAITING_MODEL)
        self._log("greeting sent")
        threading.Thread(target=self._model_loop, daemon=True).start()

    def setup_context_manager(self, config_dir, context_cfg: dict | None = None, persona: dict | None = None, mode: str = "agent", technical_prompt: str = ""):
        """Initialize context manager with config, persona, mode, and technical prompt."""
        if not context_cfg:
            context_cfg = {}
        self._context_mgr = ContextManager(
            colibri=self.colibri,
            session=self.session,
            config_dir=Path(config_dir),
            threshold=context_cfg.get("threshold", 75.0),
            summary_max_tokens=context_cfg.get("summary_max_tokens", 1200),
            recent_keep=context_cfg.get("recent_messages_keep", 4),
            archive_dir=context_cfg.get("archive_dir", "sessions"),
            log_fn=self._log,
        )
        if persona:
            self._context_mgr.set_persona(persona, persona_dir=Path(config_dir) / "persona")
        self._context_mgr.set_mode(mode)
        self._context_mgr.set_technical_prompt(technical_prompt)
        self._log(
            f"context_manager: threshold={self._context_mgr.threshold}% "
            f"summary_max={self._context_mgr.summary_max_tokens} "
            f"recent_keep={self._context_mgr.recent_keep}"
        )
        if persona:
            self._log(f"persona: {persona.get('name', '?')} bound to context_manager")

    def _check_context_rotation(self):
        """Check if context needs compaction and rotate if so.

        Called after each idle transition. Safe: does nothing if
        context manager is not set up or context is fine.
        """
        if not self._context_mgr:
            return
        # Guard: don't rotate if agent is no longer idle (user sent a new message)
        with self._lock:
            if self._state != AgentState.IDLE:
                return
        try:
            if not self._context_mgr.needs_compaction():
                return
        except Exception as exc:
            self._log(f"context check error: {exc}")
            return

        # Re-check state before starting rotation
        with self._lock:
            if self._state != AgentState.IDLE:
                self._log("rotation skipped: agent no longer idle")
                return
        self._log("starting context rotation...")
        self._set_state(AgentState.COMPACTING)
        result = self._context_mgr.rotate()
        if result["success"]:
            msg = (
                f"🔄 Context rotated: {result['before_tokens']} → "
                f"~{result['after_tokens']} tokens"
            )
            self._log(msg)
            self.on_compaction(result)
        else:
            self._log(f"rotation failed: {result['error']}")
        self._set_state(AgentState.IDLE)

    # ── Internal methods ────────────────────────────────────────

    def _set_state(self, state: str):
        self._state = state
        self._save_state()
        self.on_status(state)

    def _model_loop(self):
        """Main loop: model → command → (approve | auto) → result."""
        while not self._stop_event.is_set():
            try:
                content = self._call_model()
            except Exception as exc:
                self.on_chat(f"Error: {exc}", role="assistant")
                self._log(f"error: {exc}")
                with self._lock:
                    self._set_state(AgentState.IDLE)
                return

            if self._stop_event.is_set():
                return

            # Parse ```cmd``` block from text (skip in chat mode — no commands)
            cmd_match = None
            command = None
            if content and self.mode != "chat":
                cmd_match = re.search(r"```cmd\s*\n(.*?)\n```", content, re.DOTALL)
                if cmd_match:
                    command = cmd_match.group(1).strip()
                else:
                    # Fuzzy: model opened ```cmd but forgot closing ```
                    fuzzy = re.search(r"```cmd\s*\n(.+?)(?:\n```|\n`$|$)", content, re.DOTALL)
                    if fuzzy:
                        command = fuzzy.group(1).strip()
                        self._log(f"fuzzy cmd match: {command[:80]}")
                        # Fix content: add closing ``` so chat display is clean
                        if "```cmd" in content and content.rstrip().endswith("`"):
                            # Single trailing backtick — replace with triple
                            content = content.rstrip()[:-1] + "```"
                        elif "```cmd" in content and not content.rstrip().endswith("```"):
                            # No closing at all — append it
                            content = content.rstrip() + "\n```"
                        # Update cmd_match so downstream code treats it as normal
                        cmd_match = re.search(r"```cmd\s*\n(.*?)\n```", content, re.DOTALL)

            # on_chat — finishes streaming (prints remainder, resets)
            if content:
                self.on_chat(content, role="assistant")
                self._log(f"assistant: {content[:200]}")

            if not cmd_match:
                if not content or not content.strip():
                    # Empty response — tell model and retry (max 2 times)
                    self._empty_retries = getattr(self, '_empty_retries', 0) + 1
                    if self._empty_retries > 2:
                        self._log("empty response after 2 retries, giving up")
                        self._empty_retries = 0
                        with self._lock:
                            self.session.history.append({
                                "role": "assistant",
                                "content": "",
                            })
                            self.session.save()
                            self._set_state(AgentState.IDLE)
                        return
                    self._log(f"empty response, asking model to retry ({self._empty_retries}/2)")
                    with self._lock:
                        self.session.history.append({
                            "role": "user",
                            "content": "[Your response was empty. Repeat your response as text.]",
                        })
                        self.session.save()
                        self._set_state(AgentState.WAITING_MODEL)
                    continue
                self._empty_retries = 0
                with self._lock:
                    self.session.history.append({
                        "role": "assistant",
                        "content": content or "",
                    })
                    self.session.save()
                    self._set_state(AgentState.IDLE)
                self._check_context_rotation()
                return

            command = cmd_match.group(1).strip()

            try:
                if not command or len(command) > 4000:
                    raise RuntimeError("Empty or too long command.")
                # Shell injection guard — block only dangerous chaining,
                # allow pipe (|) for legitimate commands like grep, awk
                dangerous = [";", "&&", "||", "\n", "\r", "`", "$("]
                for tok in dangerous:
                    if tok in command:
                        raise RuntimeError(f"Shell operator '{tok}' blocked in command.")
            except Exception as exc:
                self.on_chat(f"Error: {exc}", role="assistant")
                self._log(f"parse error: {exc}")
                with self._lock:
                    self._set_state(AgentState.IDLE)
                return

            normalized = " ".join(command.split())

            with self._lock:
                # Command limit (0 = unlimited)
                if self.max_steps > 0 and self._commands_run >= self.max_steps:
                    self.session.history.append({"role": "assistant", "content": content or ""})
                    self.session.history.append({"role": "user", "content": f"[Command limit reached ({self.max_steps}). Don't run more commands. Answer based on what you have.]"})
                    self.session.save()
                    self._log(f"command limit reached: {self._commands_run}/{self.max_steps}")
                    self._set_state(AgentState.WAITING_MODEL)
                    continue
                # Duplicate
                if normalized in self._commands_seen:
                    self.session.history.append({"role": "assistant", "content": content or ""})
                    self.session.history.append({"role": "user", "content": "[Duplicate command blocked.]"})
                    self.session.save()
                    self._set_state(AgentState.IDLE)
                    self.on_output({"error": "Duplicate command blocked."})
                    return
                # Save assistant message
                self.session.history.append({
                    "role": "assistant",
                    "content": content or "",
                })
                self.session.save()
                self._pending_command = command

            # Approve?
            if needs_approval(command, self.permission_mode):
                with self._lock:
                    self._approval_event = threading.Event()
                    self._set_state(AgentState.WAITING_APPROVAL)
                self._log(f"command proposed: {command}")
                # Show approval prompt in chat or cmd panel
                if self.approval_location != "cmd":
                    self.on_command(command)
                # If approval_location == "cmd", only .state.json is used
                # (cmd panel reads it and shows the prompt)
                while not self._approval_event.wait(timeout=0.5):
                    if self._stop_event.is_set():
                        return
                if self._stop_event.is_set():
                    return
                if not self._approval_result:
                    self._record_result(f"[Command rejected: {command}]")
                    self.on_output({"rejected": True})
                    self._log("command rejected")
                    with self._lock:
                        self._pending_command = None
                        self._set_state(AgentState.IDLE)
                    return
                ok = self._run_pending_command()
                if not ok or not self.auto_continue or self._stop_event.is_set():
                    return
                with self._lock:
                    self._set_state(AgentState.WAITING_MODEL)
            else:
                ok = self._run_pending_command()
                if not ok or not self.auto_continue or self._stop_event.is_set():
                    return
                with self._lock:
                    self._set_state(AgentState.WAITING_MODEL)

    def _run_pending_command(self) -> bool:
        """Execute _pending_command. Returns True if OK."""
        with self._lock:
            command = self._pending_command
            if command is None:
                return False
            self._commands_seen.add(" ".join(command.split()))
            self._commands_run += 1
            self._set_state(AgentState.RUNNING_COMMAND)
        self._log(f"running: {command}")
        result = self._run_subprocess(command)
        if result.get("cancelled"):
            with self._lock:
                self._record_result(json.dumps(result, ensure_ascii=False))
                self._pending_command = None
            self.on_output(result)
            self._log("cancelled")
            return False
        with self._lock:
            self._last_result = result
            result_text = f"[exit={result.get('exit_code', '?')}]"
            if result.get("stdout"):
                result_text += f"\n{result['stdout'][:500]}"
            if result.get("stderr"):
                result_text += f"\nSTDERR: {result['stderr'][:500]}"
            self._record_result(result_text)
            self._pending_command = None
            self._set_state(AgentState.WAITING_DECISION)
        self.on_output(result)
        self._log(f"done: exit={result.get('exit_code')}")
        return True

    def _call_model(self) -> str | None:
        """Call Colibri with full conversation history."""
        with self._lock:
            messages = list(self.session.history)
        try:
            text_parts = self._stream_model(messages)
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:200]
            raise RuntimeError(f"Colibri HTTP {exc.code}: {detail}") from exc
        except Exception as exc:
            raise RuntimeError(f"Colibri error: {exc}") from exc
        return "".join(text_parts).strip() or None

    def _stream_model(self, messages):
        text_parts: list[str] = []
        for event in self.colibri.chat_stream(messages, None):
            if self._stop_event.is_set():
                return text_parts
            etype = event["type"]
            if etype == "text":
                text_parts.append(event["content"])
                self.on_chat_chunk(event["content"])
            elif etype == "done":
                break
        return text_parts

    def _record_result(self, text: str):
        """Record command result in history as user message."""
        self.session.history.append({
            "role": "user",
            "content": text,
        })
        self.session.save()

    def _run_subprocess(self, command: str) -> dict:
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            opts = {"cwd": str(self.workspace), "stdout": out, "stderr": err, "shell": True}
            if os.name == "posix":
                opts["start_new_session"] = True
            elif os.name == "nt":
                opts["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            proc = subprocess.Popen(command, **opts)
            with self._lock:
                self._current_process = proc
            deadline = time.monotonic() + self.command_timeout
            timed_out = False
            while proc.poll() is None:
                if self._stop_event.is_set():
                    terminate_process(proc)
                    with self._lock:
                        self._current_process = None
                    return {"cancelled": True}
                if time.monotonic() >= deadline:
                    timed_out = True
                    terminate_process(proc)
                    break
                time.sleep(0.2)
            proc.wait()
            with self._lock:
                self._current_process = None
            if self._stop_event.is_set():
                return {"cancelled": True}
            return {
                "exit_code": proc.returncode,
                "stdout": tail_text(out),
                "stderr": tail_text(err),
                "timed_out": timed_out,
            }

    def close(self):
        """Close resources."""
        try:
            self._log_file.close()
        except Exception:
            pass