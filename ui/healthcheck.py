#!/usr/bin/env python3
"""Healthcheck panel for tmux.

Shows:
- Colibri availability (ping /v1/models)
- Response latency (ms)
- Agent state (.state.json)
- Command counter
- Context usage bar
- Recent events from .agent.log (compact)
"""

import json
import sys
import time
import urllib.request
from pathlib import Path


def _find_config():
    """Find config.json: from --config arg or relative to project root."""
    import argparse
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--config", default=None)
    args, _ = parser.parse_known_args()
    if args.config:
        return Path(args.config).resolve()
    return Path(__file__).resolve().parent.parent / "config.json"


CONFIG_PATH = _find_config()


def load_config():
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return {}


def check_colibri(base_url, api_key):
    """Ping Colibri, return (ok, latency_ms, model)."""
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        t0 = time.monotonic()
        req = urllib.request.Request(f"{base_url}/models", headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.load(resp)
            latency = (time.monotonic() - t0) * 1000
            models = [m.get("id", "?") for m in data.get("data", [])]
            return True, latency, models[0] if models else "?"
    except Exception as exc:
        return False, 0, str(exc)[:60]


def _get_health(server_url, api_key):
    """Fetch /health endpoint."""
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(f"{server_url}/health", headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=3) as resp:
        return json.load(resp)


def _get_profile_tokens(server_url, api_key):
    """Get prompt_tokens from last completed turn in /profile."""
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(f"{server_url}/profile", headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=3) as resp:
        profile = json.load(resp)
    turns = profile.get("turns") or []
    if turns:
        return int(turns[-1].get("prompt_tokens", 0))
    return 0


def read_state(workspace):
    state_path = workspace / ".state.json"
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def tail_log(workspace, n=6):
    log_path = workspace / ".agent.log"
    try:
        lines = log_path.read_text(encoding="utf-8").splitlines()
        return lines[-n:]
    except Exception:
        return []


def render(config, workspace):
    colibri_cfg = config.get("colibri", {})
    base_url = colibri_cfg.get("base_url", "http://127.0.0.1:8000/v1")
    api_key = colibri_cfg.get("api_key", "")

    ok, latency, model = check_colibri(base_url, api_key)
    if ok:
        status_line = f"✅ Colibri: {model} | {latency:.0f}ms"
    else:
        status_line = f"❌ Colibri: {model}"

    state = read_state(workspace)
    agent_state = state.get("state", "idle")
    cmd_count = state.get("commands_run", 0)
    mode = state.get("permission_mode", "?")
    auto = "on" if state.get("auto_continue") else "off"
    pending = state.get("pending_command")

    state_icons = {
        "idle": "⬜ idle",
        "waiting_model": "🤖 model",
        "waiting_approval": "⏳ approval",
        "running_command": "⚡ running",
        "waiting_decision": "📝 decision",
        "compacting": "🔄 compacting",
    }
    state_str = state_icons.get(agent_state, agent_state)

    # Context usage from /health
    context_line = ""
    context_cfg = config.get("context", {})
    threshold = context_cfg.get("threshold", 75)
    try:
        server_url = base_url.rstrip("/")
        if server_url.endswith("/v1"):
            server_url = server_url[:-3]
        health = _get_health(server_url, api_key)
        ctx_len = int(health.get("context_tokens", 0))
        live = health.get("live") or []
        if live:
            used = int(live[0].get("prompt_tokens", 0)) + int(live[0].get("tokens", 0))
        else:
            # Fallback: /profile last turn prompt_tokens = context size at last request
            used = _get_profile_tokens(server_url, api_key)
        if ctx_len > 0 and used > 0:
            pct = used * 100 / ctx_len
            bar_len = 20
            filled = int(bar_len * pct / 100)
            bar = "█" * filled + "░" * (bar_len - filled)
            marker = " ⚠️" if pct >= threshold else ""
            context_line = f"  Context: [{bar}] {pct:.1f}% ({used}/{ctx_len}){marker}"
    except Exception:
        pass

    log_lines = tail_log(workspace, 6)

    # Clear screen with ANSI escape (no fork+exec)
    sys.stdout.write("\033[2J\033[H")
    sys.stdout.flush()
    print("╔══ Health ══╗")
    print()
    print(f"  {status_line}")
    print(f"  State: {state_str} | cmds: {cmd_count} | mode: {mode} | auto: {auto}")
    if context_line:
        print(context_line)
    if pending and agent_state == "waiting_approval":
        print(f"  ⏳ Pending: {pending[:80]}")
    print()
    print("─ log ─")
    for line in log_lines:
        print(f"  {line}")
    if not log_lines:
        print("  (empty)")
    print()
    print(f"  Refresh every 5s | {time.strftime('%H:%M:%S')}")


def main():
    config = load_config()
    agent_cfg = config.get("agent", {})
    workspace = Path(agent_cfg.get("workspace", "workspace"))
    if not workspace.is_absolute():
        workspace = CONFIG_PATH.parent / workspace

    while True:
        try:
            render(config, workspace)
        except Exception as exc:
            print(f"  ⚠️ {exc}")
        time.sleep(5)


if __name__ == "__main__":
    main()
