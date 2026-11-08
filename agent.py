#!/usr/bin/env python3
"""Colibri Mini Agent — entry point.

Usage:
    python3 agent.py          — tmux with three panels
    python3 agent.py chat     — chat panel only (agent)
    python3 agent.py commands  — commands panel only
    python3 agent.py log      — log only
    python3 agent.py health   — healthcheck only

Tmux panels:
    1. chat     — agent + dialog (input + streaming)
    2. commands — approve commands (reads .state.json, writes to .cmd_fifo)
    3. health   — Colibri status, agent state, recent log
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from core.colibri_client import ColibriClient
from core.persona import load_persona, build_system_prompt
from core.session import Session
from core.permissions import MODES


DEFAULT_MODEL = "your-model-name"
DEFAULT_SYSTEM_PROMPT = (
    "You are a local agent. Be brief, no reasoning. "
    "To run a command write:\n"
    "```cmd\n"
    "command\n"
    "```\n"
    "One command at a time. Don't repeat without reason. "
    "If no command needed — just reply with text. Don't explain, just run."
)


def load_persona_system_prompt(config_path: Path, mode: str = "rp_agent") -> tuple[str, dict]:
    """Load system prompt from persona/ directory.

    Returns (system_prompt, persona_dict).
    Falls back to DEFAULT_SYSTEM_PROMPT if persona dir doesn't exist.
    """
    persona_dir = config_path.parent / "persona"
    if not persona_dir.exists():
        return DEFAULT_SYSTEM_PROMPT, {}
    persona = load_persona(persona_dir)
    # Load technical prompt from config
    config = json.loads(config_path.read_text(encoding="utf-8"))
    technical_prompt = config.get("prompt", {}).get("system", "")
    system_prompt = build_system_prompt(
        persona, context_block="", mode=mode,
        technical_prompt=technical_prompt, persona_dir=persona_dir,
    )
    return system_prompt, persona


def load_config(path: str) -> tuple[dict, Path]:
    config_path = Path(path).expanduser().resolve()
    if not config_path.is_file():
        return {}, config_path
    data = json.loads(config_path.read_text(encoding="utf-8"))
    return data, config_path


def create_agent(args, config, config_path):
    """Create agent from config and CLI args."""
    mode = config.get("mode", "agent")
    # Valid modes: agent, rp_agent, chat
    if mode not in ("agent", "rp_agent", "chat"):
        print(f"⚠️ Unknown mode '{mode}', falling back to 'agent'")
        mode = "agent"

    persona = {}
    if mode in ("rp_agent", "chat"):
        # Load persona system prompt
        system_prompt, persona = load_persona_system_prompt(config_path, mode)
        if persona:
            print(f"Persona: {persona.get('name', '?')} loaded")
        else:
            print(f"⚠️ mode={mode} but no persona/ directory found, falling back to agent")
            mode = "agent"

    if mode == "agent":
        system_prompt = config.get("prompt", {}).get("system", DEFAULT_SYSTEM_PROMPT)

    tools = config.get("tools") or None

    colibri_cfg = config.get("colibri", {})
    colibri_url = args.colibri or colibri_cfg.get("base_url", "http://127.0.0.1:8000/v1")
    api_key = args.api_key if args.api_key is not None else colibri_cfg.get("api_key", "")
    model = args.model or colibri_cfg.get("model", DEFAULT_MODEL)

    agent_cfg = config.get("agent", {})
    workspace_val = args.workspace or agent_cfg.get("workspace", "workspace")
    workspace = Path(workspace_val).expanduser()
    if not workspace.is_absolute():
        workspace = config_path.parent / workspace
    workspace.mkdir(parents=True, exist_ok=True)

    command_timeout = args.command_timeout or agent_cfg.get("command_timeout", 60)
    max_steps = args.max_steps or agent_cfg.get("max_steps", 3)
    max_tokens = args.max_tokens or agent_cfg.get("max_tokens")
    permission_mode = args.permission_mode or agent_cfg.get("permission_mode", "ask")
    approval_location = agent_cfg.get("approval_location", "chat")
    auto_continue = args.auto_continue if args.auto_continue is not None else agent_cfg.get("auto_continue", False)
    max_history_messages = agent_cfg.get("max_history_messages", 40)
    max_history_chars = agent_cfg.get("max_history_chars", 120_000)

    session_val = agent_cfg.get("session_file", "session.json")
    session_file = Path(session_val).expanduser() if session_val else None
    if session_file and not session_file.is_absolute():
        session_file = config_path.parent / session_val

    cache_slot = colibri_cfg.get("cache_slot", 0)
    temperature = colibri_cfg.get("temperature", 0.7)
    enable_thinking = colibri_cfg.get("enable_thinking", False)
    colibri = ColibriClient(colibri_url, api_key, model, max_tokens,
                            cache_slot=cache_slot, temperature=temperature,
                            enable_thinking=enable_thinking)

    # Check Colibri connection
    print(f"Colibri: {colibri_url} | model: {model}")
    print(f"Mode: {mode}")
    print(f"Workspace: {workspace}")
    print(f"Permissions: {permission_mode} | auto_continue: {auto_continue}")
    print("Checking Colibri...", end=" ", flush=True)
    ok, msg = colibri.check_connection()
    if ok:
        print(f"✅ {msg}")
    else:
        print(f"❌ {msg}")
        print(f"\nColibri unavailable. Check: {colibri_url}")
        sys.exit(1)
    print()

    fresh_start = agent_cfg.get("fresh_start", False)
    if fresh_start and session_file and session_file.exists():
        session_file.unlink()
        print("Session reset (fresh_start=true)")
    session = Session(system_prompt, session_file, max_history_messages, max_history_chars)

    from core.agent import Agent
    agent = Agent(
        colibri=colibri,
        system_prompt=system_prompt,
        tools=tools,
        workspace=workspace,
        session=session,
        command_timeout=command_timeout,
        max_steps=max_steps,
        permission_mode=permission_mode,
        auto_continue=auto_continue,
        mode=mode,
        approval_location=approval_location,
    )

    # Context manager
    context_cfg = config.get("context", {})
    if context_cfg.get("enabled", True):
        technical_prompt = config.get("prompt", {}).get("system", "")
        agent.setup_context_manager(
            config_dir=config_path.parent,
            context_cfg=context_cfg,
            persona=persona,
            mode=mode,
            technical_prompt=technical_prompt,
        )

    # Greeting: in rp_agent and chat modes, model starts the conversation
    # Only if session is fresh (just system prompt) or context is small (< 500 tokens)
    if mode in ("rp_agent", "chat"):
        history_len = len(agent.session.history)
        # Check actual context size if context manager is available
        skip_greeting = False
        if history_len > 1 and agent._context_mgr:
            try:
                ctx = agent._context_mgr.read_context()
                if ctx["total_tokens"] > 500:
                    skip_greeting = True
                    print(f"Session has {ctx['total_tokens']} tokens, skipping greeting")
            except Exception:
                pass
        if not skip_greeting and history_len <= 1:
            agent.start_greeting()
        elif not skip_greeting:
            print(f"Session has {history_len} messages, skipping greeting")

    return agent


def run_chat(args, config, config_path):
    """Run chat client (agent lives here)."""
    from ui.chat_client import ChatClient
    agent = create_agent(args, config, config_path)
    ChatClient(agent).run()


def run_commands(args, config, config_path):
    """Run commands client."""
    from ui.commands_client import CommandsClient
    agent_cfg = config.get("agent", {})
    workspace_val = args.workspace or agent_cfg.get("workspace", "workspace")
    workspace = Path(workspace_val).expanduser()
    if not workspace.is_absolute():
        workspace = config_path.parent / workspace
    workspace.mkdir(parents=True, exist_ok=True)
    CommandsClient(workspace).run()


def run_log(args, config, config_path):
    """Run tail -f .agent.log."""
    agent_cfg = config.get("agent", {})
    workspace_val = args.workspace or agent_cfg.get("workspace", "workspace")
    workspace = Path(workspace_val).expanduser()
    if not workspace.is_absolute():
        workspace = config_path.parent / workspace
    log_path = workspace / ".agent.log"
    print(f"╔══ Log: {log_path} ══╗")
    print()
    try:
        subprocess.run(["tail", "-f", str(log_path)])
    except FileNotFoundError:
        print(f"Log not found: {log_path}")
        sys.exit(1)


def run_tmux(args, config, config_path):
    """Run tmux with three panels."""
    try:
        subprocess.run(["tmux", "-V"], capture_output=True, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        print("tmux not installed. Install: apt install tmux")
        sys.exit(1)

    agent_cfg = config.get("agent", {})
    workspace_val = args.workspace or agent_cfg.get("workspace", "workspace")
    workspace = Path(workspace_val).expanduser()
    if not workspace.is_absolute():
        workspace = config_path.parent / workspace_val
    workspace.mkdir(parents=True, exist_ok=True)

    # Clean up stale IPC files
    fifo_path = workspace / ".cmd_fifo"
    if fifo_path.exists() and not os.path.exists(fifo_path):
        fifo_path.unlink(missing_ok=True)

    # Check Colibri before starting tmux
    colibri_cfg = config.get("colibri", {})
    colibri_url = args.colibri or colibri_cfg.get("base_url", "http://127.0.0.1:8000/v1")
    api_key = args.api_key if args.api_key is not None else colibri_cfg.get("api_key", "")
    model = args.model or colibri_cfg.get("model", DEFAULT_MODEL)

    colibri = ColibriClient(colibri_url, api_key, model,
                            agent_cfg.get("max_tokens"),
                            cache_slot=colibri_cfg.get("cache_slot", 0),
                            temperature=colibri_cfg.get("temperature", 0.7),
                            enable_thinking=colibri_cfg.get("enable_thinking", False))
    print(f"Colibri: {colibri_url} | model: {model}")
    print(f"Workspace: {workspace}")
    print("Checking Colibri...", end=" ", flush=True)
    ok, msg = colibri.check_connection()
    if ok:
        print(f"✅ {msg}")
    else:
        print(f"❌ {msg}")
        print(f"\nColibri unavailable. Check: {colibri_url}")
        sys.exit(1)
    print()

    script = Path(__file__).resolve()
    config_str = str(config_path)
    tmux_conf = Path(__file__).resolve().parent / ".tmux.conf"

    tmux_cmds = [
        # New session — panel 0 (chat) with custom config
        ["tmux", "new-session", "-d", "-s", "colibri-agent",
         "-f", str(tmux_conf),
         "python3", str(script), "chat", "--config", config_str],

        # Split horizontally: chat | right (panel 1 = commands)
        ["tmux", "split-window", "-h", "-t", "colibri-agent",
         "python3", str(script), "commands", "--config", config_str],

        # Split panel 1 vertically: commands / health (panel 2)
        ["tmux", "split-window", "-v", "-t", "colibri-agent:0.1",
         "python3", str(script), "health", "--config", config_str],

        # Resize: chat — 66%, right — 34%
        ["tmux", "resize-pane", "-t", "colibri-agent:0.0", "-x", "66%"],
        ["tmux", "resize-pane", "-t", "colibri-agent:0.1", "-x", "34%"],
        ["tmux", "resize-pane", "-t", "colibri-agent:0.2", "-x", "34%"],

        # Focus on chat
        ["tmux", "select-pane", "-t", "colibri-agent:0.0"],

        # Attach
        ["tmux", "attach-session", "-t", "colibri-agent"],
    ]

    for cmd in tmux_cmds[:-1]:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"tmux error: {result.stderr}", file=sys.stderr)

    try:
        subprocess.run(tmux_cmds[-1])
    except KeyboardInterrupt:
        pass


def main():
    parser = argparse.ArgumentParser(
        description="Colibri Mini Agent — tmux panels.",
    )
    subparsers = parser.add_subparsers(dest="command")

    tmux_parser = subparsers.add_parser("tmux", help="Run tmux with 3 panels")
    _add_common_args(tmux_parser)

    chat_parser = subparsers.add_parser("chat", help="Chat panel (agent)")
    _add_common_args(chat_parser)

    cmd_parser = subparsers.add_parser("commands", help="Commands panel")
    _add_common_args(cmd_parser)

    log_parser = subparsers.add_parser("log", help="Log")
    _add_common_args(log_parser)

    health_parser = subparsers.add_parser("health", help="Healthcheck")
    _add_common_args(health_parser)

    args = parser.parse_args()
    if args.command is None:
        import sys as _sys
        _sys.argv.insert(1, "tmux")
        args = parser.parse_args()

    config, config_path = load_config(
        args.config if hasattr(args, "config") else str(Path(__file__).with_name("config.json"))
    )

    if args.command == "tmux":
        run_tmux(args, config, config_path)
    elif args.command == "chat":
        run_chat(args, config, config_path)
    elif args.command == "commands":
        run_commands(args, config, config_path)
    elif args.command == "log":
        run_log(args, config, config_path)
    elif args.command == "health":
        from ui.healthcheck import main as health_main
        health_main()


def _add_common_args(p):
    p.add_argument("--config", default=str(Path(__file__).with_name("config.json")))
    p.add_argument("--colibri")
    p.add_argument("--api-key")
    p.add_argument("--model")
    p.add_argument("--workspace")
    p.add_argument("--command-timeout", type=int)
    p.add_argument("--max-steps", type=int)
    p.add_argument("--max-tokens", type=int)
    p.add_argument("--permission-mode", choices=MODES)
    p.add_argument("--auto-continue", action="store_true", default=None)
    p.add_argument("--no-auto-continue", dest="auto_continue", action="store_false")


if __name__ == "__main__":
    main()