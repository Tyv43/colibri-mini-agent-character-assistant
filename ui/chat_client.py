"""Chat client for tmux panel.

input() + streaming print(). Agent lives here.
"""

import sys
import threading
import time

from core.agent import Agent


class ChatClient:
    """Chat panel. Agent lives here. input() + streaming."""

    def __init__(self, agent: Agent):
        self.agent = agent
        self._streaming = False
        self._lock = threading.Lock()
        self._running = True
        self._cmd_filter_buffer = ""
        self._cmd_block_active = False
        self._thinking = False

        agent.on_chat = self._on_chat
        agent.on_chat_chunk = self._on_chat_chunk
        agent.on_command = self._on_command
        agent.on_output = self._on_output
        agent.on_status = self._on_status
        agent.on_done = self._on_done

    def _print(self, text=""):
        with self._lock:
            print(text, flush=True)

    def _write(self, text):
        with self._lock:
            sys.stdout.write(text)
            sys.stdout.flush()

    # ── Callbacks ───────────────────────────────────────────────

    def _on_chat(self, text, role="assistant"):
        if role == "user":
            return
        if self._thinking:
            self._write("\r" + " " * 25 + "\r")
            self._thinking = False
        # Flush any remaining filter buffer (text before cmd block that wasn't printed)
        if self._cmd_filter_buffer and not self._cmd_block_active:
            if not self._streaming:
                self._streaming = True
                ts = time.strftime("[%H:%M]")
                self._write(f"\n{ts} Agent: ")
            self._write(self._cmd_filter_buffer)
        self._cmd_filter_buffer = ""
        self._cmd_block_active = False
        if self._streaming:
            self._print()
            self._streaming = False

    def _on_chat_chunk(self, text):
        # Clear "Thinking..." on first chunk
        if self._thinking:
            self._write("\r" + " " * 25 + "\r")
            self._thinking = False
        self._cmd_filter_buffer += text

        # If in cmd-block mode — look for closing ```
        if self._cmd_block_active:
            if "```" in self._cmd_filter_buffer:
                # Found closing — extract text after it
                parts = self._cmd_filter_buffer.split("```", 1)
                after = parts[1] if len(parts) > 1 else ""
                self._cmd_block_active = False
                self._cmd_filter_buffer = after.lstrip("\n")
                if self._cmd_filter_buffer:
                    if not self._streaming:
                        self._streaming = True
                        ts = time.strftime("[%H:%M]")
                        self._write(f"\n{ts} Agent: ")
                    self._write(self._cmd_filter_buffer)
                    self._cmd_filter_buffer = ""
            return

        # Not in cmd-block — look for start of ```cmd
        if "```cmd" in self._cmd_filter_buffer:
            parts = self._cmd_filter_buffer.split("```cmd", 1)
            before = parts[0]
            after = parts[1] if len(parts) > 1 else ""
            if before:
                if not self._streaming:
                    self._streaming = True
                    ts = time.strftime("[%H:%M]")
                    self._write(f"\n{ts} Agent: ")
                self._write(before)
            self._cmd_block_active = True
            # Check if closing is also in this chunk (inline cmd block)
            if "```" in after:
                parts2 = after.split("```", 1)
                remaining = parts2[1] if len(parts2) > 1 else ""
                self._cmd_block_active = False
                self._cmd_filter_buffer = remaining.lstrip("\n")
                if self._cmd_filter_buffer:
                    self._write(self._cmd_filter_buffer)
                    self._cmd_filter_buffer = ""
            elif after.strip() == "`":
                # Fuzzy close — single backtick
                self._cmd_block_active = False
                self._cmd_filter_buffer = ""
            else:
                self._cmd_filter_buffer = after
            return

        # No cmd-block. But might be start of ` — hold off
        if self._cmd_filter_buffer.endswith("`") or self._cmd_filter_buffer.endswith("``"):
            return

        # Regular text — print (with header if first time)
        if self._cmd_filter_buffer:
            if not self._streaming:
                self._streaming = True
                ts = time.strftime("[%H:%M]")
                self._write(f"\n{ts} Agent: ")
            self._write(self._cmd_filter_buffer)
            self._cmd_filter_buffer = ""

    def _on_command(self, command):
        if self._thinking:
            self._write("\r" + " " * 25 + "\r")
            self._thinking = False
        if self._streaming:
            if self._cmd_filter_buffer and not self._cmd_block_active:
                self._write(self._cmd_filter_buffer)
            self._print()
            self._streaming = False
        self._cmd_filter_buffer = ""
        self._cmd_block_active = False
        self._print(f"📋 $ {command}")
        self._print("   y — approve, n — reject")

    def _on_output(self, result):
        if self._thinking:
            self._write("\r" + " " * 25 + "\r")
            self._thinking = False
        if self._streaming:
            self._print()
            self._streaming = False
        self._cmd_filter_buffer = ""
        self._cmd_block_active = False

    def _on_status(self, state):
        if state == "waiting_model" and not self._streaming:
            self._write("\r🤖 Thinking...   ")
            self._thinking = True
        elif state == "compacting":
            if self._thinking:
                self._write("\r" + " " * 25 + "\r")
                self._thinking = False
            self._write("\r🔄 Compacting...   ")
        elif state in ("idle", "stopped"):
            if self._thinking:
                self._write("\r" + " " * 25 + "\r")
                self._thinking = False
            # Reset streaming state — prevents lost messages after stop/reset
            if self._streaming:
                self._print()
                self._streaming = False
            self._cmd_filter_buffer = ""
            self._cmd_block_active = False
            self._write("\rYou: ")

    def _on_done(self):
        self._print("\n   ✅ Task completed")

    # ── Agent thread runner ─────────────────────────────────────

    def _run_async(self, fn, *args):
        def wrapper():
            try:
                fn(*args)
            except Exception as exc:
                self._print(f"\n   ⚠️ {exc}")
        threading.Thread(target=wrapper, daemon=True).start()

    def _send_halt(self):
        """Send halt via FIFO."""
        try:
            with open(self.agent.fifo_path, "w") as f:
                f.write("halt\n")
                f.flush()
            self._print("   ⏹ Cycle halted")
        except Exception as exc:
            self._print(f"   ⚠️ {exc}")

    def _save_dialog(self):
        """Save and compact current dialog — archive + summary, like rotation."""
        if not self.agent._context_mgr:
            self._print("   ⚠️ Context manager not available")
            return
        if self.agent.state != "idle":
            self._print("   ⚠️ Agent busy, stop first")
            return
        if self.agent._context_mgr._rotating:
            self._print("   ⚠️ Already saving/rotating, wait...")
            return
        self._print("   💾 Saving dialog...")
        def _do_save():
            with self.agent._lock:
                if self.agent._state != "idle":
                    self._print("   ⚠️ Agent became busy, aborting save")
                    return
                self.agent._set_state("compacting")
            result = self.agent._context_mgr.rotate()
            if result["success"]:
                self._print(f"   ✅ Saved: {result['before_tokens']} → ~{result['after_tokens']} tokens")
                self._print(f"   📁 Archive: {result['archive_path']}")
            else:
                self._print(f"   ⚠️ Save failed: {result['error']}")
            with self.agent._lock:
                self.agent._set_state("idle")
        threading.Thread(target=_do_save, daemon=True).start()

    def _list_summaries(self) -> list:
        """List available summary files. Returns list of (num, path, info)."""
        archive_path = self.agent._context_mgr.archive_path
        if not archive_path.exists():
            self._print("   No saved dialogs")
            return []
        summaries = sorted(archive_path.glob("summary_*.md"))
        if not summaries:
            self._print("   No saved dialogs")
            return []
        items = []
        self._print("   Saved dialogs:")
        for i, spath in enumerate(summaries, 1):
            # Parse info from first 3 lines
            try:
                lines = spath.read_text(encoding="utf-8").splitlines()
                ts = lines[0].replace("# Context Summary — ", "") if lines else "?"
                before = lines[1].replace("Before: ", "") if len(lines) > 1 else "?"
                summary_size = lines[2].replace("Summary: ", "") if len(lines) > 2 else "?"
                self._print(f"   {i}. [{ts}] before={before}, summary={summary_size}")
                items.append((i, spath, {"ts": ts, "before": before, "summary": summary_size}))
            except Exception:
                self._print(f"   {i}. {spath.name} (parse error)")
                items.append((i, spath, {}))
        self._print("   /load N — load dialog N")
        return items

    def _load_dialog(self, num: int | None = None):
        """Load a saved dialog by number, or list if num is None."""
        if not self.agent._context_mgr:
            self._print("   ⚠️ Context manager not available")
            return
        if self.agent.state != "idle":
            self._print("   ⚠️ Agent busy, stop first")
            return

        if num is None:
            self._list_summaries()
            return

        items = self._list_summaries()
        if not items:
            return
        if num < 1 or num > len(items):
            self._print(f"   ⚠️ Invalid number (1-{len(items)})")
            return

        spath = items[num - 1][1]
        self._print(f"   Loading: {spath.name}...")

        def _do_load():
            try:
                # Read summary content (skip first 3 metadata lines)
                lines = spath.read_text(encoding="utf-8").splitlines()
                # Find where summary content starts (after blank line)
                content_start = 4  # line 0: title, 1: before, 2: summary size, 3: blank
                summary_text = "\n".join(lines[content_start:]).strip()
                if not summary_text:
                    # Fallback — use all non-metadata lines
                    summary_text = "\n".join(lines[4:]).strip()

                if not summary_text:
                    self._print("   ⚠️ Summary is empty")
                    return

                # Build new session with restored context
                from core.persona import build_context_restore, build_system_prompt

                cm = self.agent._context_mgr
                persona = cm._persona
                if persona:
                    restore = build_context_restore(persona, summary_text)
                    mode = getattr(cm, '_mode', 'rp_agent')
                    technical = getattr(cm, '_technical_prompt', '')
                    pdir = getattr(cm, '_persona_dir', None)
                    new_system = build_system_prompt(
                        persona, context_block=restore,
                        mode=mode, technical_prompt=technical, persona_dir=pdir,
                    )
                else:
                    new_system = self.agent.system_prompt + "\n\n" + summary_text

                new_history = [
                    {"role": "system", "content": new_system},
                    {"role": "assistant", "content": "Understood. Context restored. Continuing."},
                ]

                # Replace session
                self.agent.session.history = new_history
                self.agent.session.save()
                # Reset command counters
                with self.agent._lock:
                    self.agent._commands_run = 0
                    self.agent._commands_seen.clear()
                    self.agent._empty_retries = 0

                self._print(f"   ✅ Loaded: {spath.name}")
                self._print("   Ready to continue. Type your message.")
                self._write("\rYou: ")
            except Exception as exc:
                self._print(f"   ⚠️ Load failed: {exc}")

        threading.Thread(target=_do_load, daemon=True).start()

    def _handle_input(self, text) -> bool:
        text = text.strip()
        if not text:
            return True
        if text == "/quit":
            self._running = False
            return False

        if text == "/stop":
            self._run_async(self.agent.stop)
            return True
        if text == "/halt":
            self._send_halt()
            return True
        if text == "/save":
            self._save_dialog()
            return True
        if text == "/load":
            self._load_dialog()
            return True
        if text.startswith("/load "):
            num_str = text[6:].strip()
            if num_str.isdigit():
                self._load_dialog(int(num_str))
            else:
                self._print("   Usage: /load <number>")
            return True
        if text == "/help":
            self._print("\nCommands:")
            self._print("  y/n       — approve/reject command")
            self._print("  /stop     — stop agent (history preserved)")
            self._print("  /halt     — soft halt, model answers with what it has")
            self._print("  /save     — save and compact current dialog (archive + summary)")
            self._print("  /load     — list saved dialogs")
            self._print("  /load N   — load dialog N from list")
            self._print("  /reset    — reset command counter")
            self._print("  /new      — new session (history cleared)")
            self._print("  /continue — pass result back to model")
            self._print("  /done     — finish without passing to model")
            self._print("  /mode X   — ask, whitelist, allow_except_rm, allow_all")
            self._print("  /auto X   — on/off auto-continue")
            self._print("  /quit     — exit")
            return True
        if text == "/reset":
            self._run_async(self.agent.reset)
            self._print("   🔄 Reset")
            return True
        if text == "/new":
            self._run_async(self.agent.new_session)
            self._print("   ✨ New session")
            return True
        if text == "/done":
            self._run_async(self.agent.done)
            return True

        if text in ("y", "n"):
            try:
                self.agent.approve(text == "y")
            except RuntimeError as exc:
                self._print(f"   ⚠️ {exc}")
            return True

        if text == "/continue":
            try:
                self._run_async(self.agent.continue_task)
            except RuntimeError as exc:
                self._print(f"   ⚠️ {exc}")
            return True

        if text.startswith("/mode "):
            mode = text[6:].strip()
            aliases = {
                "ask": "ask", "whitelist": "whitelist", "safe": "whitelist",
                "no-rm": "allow_except_rm", "allow_except_rm": "allow_except_rm",
                "all": "allow_all", "allow_all": "allow_all",
            }
            if mode in aliases:
                try:
                    self.agent.set_mode(aliases[mode])
                    self._print(f"   Mode: {aliases[mode]}")
                except RuntimeError as exc:
                    self._print(f"   ⚠️ {exc}")
            else:
                self._print("   /mode ask, /mode whitelist, /mode no-rm, /mode all")
            return True

        if text.startswith("/auto "):
            val = text[6:].strip()
            if val in ("on", "off"):
                self.agent.set_auto_continue(val == "on")
                self._print(f"   auto_continue: {val}")
            else:
                self._print("   /auto on or /auto off")
            return True

        # Message to model
        self._run_async(self.agent.send_message, text)
        return True

    def run(self):
        self._print("╔══ Chat ══╗")
        self._print("Commands: y/n /stop /halt /reset /new /continue /done /mode /auto /help /quit")
        self._print()
        self._write("You: ")
        try:
            while self._running:
                try:
                    line = input("")
                except EOFError:
                    break
                line = line.strip()
                if not line:
                    continue
                if not self._handle_input(line):
                    break
        except KeyboardInterrupt:
            self.agent.stop()
            self._print("\nExit")
        except BrokenPipeError:
            pass
        finally:
            try:
                self.agent.stop()
                self.agent.close()
            except Exception:
                pass