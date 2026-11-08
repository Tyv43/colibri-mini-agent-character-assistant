"""Commands panel for tmux.

Reads .state.json, sends approve to .cmd_fifo.
input() + print(). Lightweight, no agent dependency.
"""

import json
import time
from pathlib import Path


class CommandsClient:
    """Commands panel. Reads state, writes to FIFO."""

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.state_path = workspace / ".state.json"
        self.fifo_path = workspace / ".cmd_fifo"
        self._running = True
        self._last_state = None
        self._last_pending = None

    def _read_state(self) -> dict:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _send_fifo(self, msg: str):
        """Write to FIFO."""
        try:
            with open(self.fifo_path, "w") as f:
                f.write(msg + "\n")
                f.flush()
        except Exception as exc:
            print(f"   ⚠️ FIFO: {exc}", flush=True)

    def _print(self, text=""):
        print(text, flush=True)

    def _handle_input(self, text) -> bool:
        text = text.strip()
        if not text:
            return True
        if text == "/quit":
            self._running = False
            return False

        state = self._read_state()
        agent_state = state.get("state", "idle")

        if text in ("y", "n"):
            if agent_state == "waiting_approval":
                self._send_fifo("approve" if text == "y" else "reject")
                self._print(f"   {'✅ Approved' if text == 'y' else '❌ Rejected'}")
            else:
                self._print(f"   ⚠️ No command (state={agent_state})")
            return True

        if text == "/continue":
            if agent_state == "waiting_decision":
                self._send_fifo("continue")
                self._print("   → To model")
            else:
                self._print(f"   ⚠️ No result (state={agent_state})")
            return True

        if text == "/done":
            if agent_state == "waiting_decision":
                self._send_fifo("done")
                self._print("   ✅ Done")
            else:
                self._print(f"   ⚠️ Not in waiting_decision (state={agent_state})")
            return True

        if text == "/stop":
            self._send_fifo("stop")
            self._print("   ⛔ Stopped")
            return True

        if text == "/halt":
            self._send_fifo("halt")
            self._print("   ⏹ Cycle halted. Model will get context.")
            return True

        if text.startswith("/mode "):
            mode = text[6:].strip()
            aliases = {
                "ask": "ask", "whitelist": "whitelist", "safe": "whitelist",
                "no-rm": "allow_except_rm", "allow_except_rm": "allow_except_rm",
                "all": "allow_all", "allow_all": "allow_all",
            }
            if mode in aliases:
                self._send_fifo(f"mode:{aliases[mode]}")
                self._print(f"   Mode: {aliases[mode]}")
            else:
                self._print("   /mode ask, /mode whitelist, /mode no-rm, /mode all")
            return True

        if text.startswith("/auto "):
            val = text[6:].strip()
            if val in ("on", "off"):
                self._send_fifo(f"auto:{val}")
                self._print(f"   auto_continue: {val}")
            else:
                self._print("   /auto on or /auto off")
            return True

        self._print(f"   Unknown command: {text}")
        return True

    def _monitor_state(self):
        """Background monitoring of state.json."""
        last_pending = None
        last_state = None
        last_result_shown = None
        while self._running:
            state = self._read_state()
            pending = state.get("pending_command")
            agent_state = state.get("state", "idle")
            last_result = state.get("last_result")

            # Show command output
            if last_result and last_result != last_result_shown:
                last_result_shown = last_result
                if last_result.get("error"):
                    self._print(f"\n⚠️ {last_result['error']}")
                elif last_result.get("rejected"):
                    self._print("\n❌ Rejected")
                elif last_result.get("cancelled"):
                    self._print("\n⛔ Cancelled")
                else:
                    exit_code = last_result.get("exit_code", "?")
                    stdout = (last_result.get("stdout") or "").strip()
                    stderr = (last_result.get("stderr") or "").strip()
                    self._print(f"\n✅ exit={exit_code}")
                    if stdout:
                        self._print("   stdout:")
                        for line in stdout.splitlines()[:20]:
                            self._print(f"   │ {line}")
                    if stderr:
                        self._print("   stderr:")
                        for line in stderr.splitlines()[:10]:
                            self._print(f"   │ {line}")
                    if not stdout and not stderr:
                        self._print("   (empty)")

            # Show pending command
            if pending and pending != last_pending and agent_state in ("waiting_approval", "running_command"):
                self._print(f"\n📋 Command: {pending}")
                if agent_state == "waiting_approval":
                    self._print("   y — approve, n — reject")
                last_pending = pending
            elif agent_state not in ("waiting_approval", "running_command"):
                last_pending = None

            # Hints
            if agent_state == "waiting_decision" and agent_state != last_state:
                self._print("\n⏳ /continue — to model, /done — finish, /stop — abort")
            elif agent_state == "waiting_model" and last_state == "waiting_decision":
                self._print("\n🤖 Model working...")

            last_state = agent_state
            time.sleep(0.3)

    def run(self):
        import threading
        monitor = threading.Thread(target=self._monitor_state, daemon=True)
        monitor.start()

        self._print("╔══ CMD status & output ══╗")
        self._print("y/n — approve, /continue, /done, /stop, /halt, /quit")
        self._print()

        try:
            while self._running:
                try:
                    line = input("cmd> ")
                except EOFError:
                    break
                if not self._handle_input(line):
                    break
        except KeyboardInterrupt:
            pass
        except BrokenPipeError:
            pass