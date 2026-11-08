#!/usr/bin/env python3
"""Config editor for Colibri Mini Agent.

Interactive TUI for editing config.json with field validation,
preset choices, and inline help.

Usage:
    python3 config_editor.py
    python3 config_editor.py --config /path/to/config.json
"""

import json
import sys
from pathlib import Path


# ── Field definitions: (key_path, label, type, choices/help) ──

FIELDS = [
    # Top-level
    {
        "path": "mode",
        "label": "Mode",
        "type": "choice",
        "choices": ["agent", "rp_agent", "chat"],
        "help": "agent — AI agent without persona\nrp_agent — persona + commands\nchat — pure roleplay, no commands",
    },
    # Colibri
    {
        "path": "colibri.base_url",
        "label": "Colibri URL",
        "type": "string",
        "help": "Colibri API URL (e.g., http://127.0.0.1:8000/v1)",
    },
    {
        "path": "colibri.api_key",
        "label": "API Key",
        "type": "string",
        "help": "API key for Colibri (usually empty for local)",
    },
    {
        "path": "colibri.model",
        "label": "Model",
        "type": "string",
        "help": "Model name (e.g., qwen3.5-9b-ds9-uss-defiant-nm-colibri)",
    },
    {
        "path": "colibri.cache_slot",
        "label": "Cache Slot",
        "type": "int",
        "help": "KV cache slot (0 = first, -1 = no cache)",
    },
    {
        "path": "colibri.temperature",
        "label": "Temperature",
        "type": "float",
        "help": "0.0 = deterministic, 1.0 = creative, 0.7 = balanced",
    },
    {
        "path": "colibri.enable_thinking",
        "label": "Enable Thinking",
        "type": "bool",
        "help": "Enable model thinking mode (if supported)",
    },
    # Agent
    {
        "path": "agent.workspace",
        "label": "Workspace",
        "type": "string",
        "help": "Working directory for the agent",
    },
    {
        "path": "agent.command_timeout",
        "label": "Command Timeout (sec)",
        "type": "int",
        "help": "Command execution timeout in seconds",
    },
    {
        "path": "agent.max_steps",
        "label": "Max Steps",
        "type": "int",
        "help": "Max commands per turn (0 = unlimited)",
    },
    {
        "path": "agent.max_tokens",
        "label": "Max Tokens",
        "type": "int_or_null",
        "help": "Max response tokens (null = no limit)",
    },
    {
        "path": "agent.permission_mode",
        "label": "Permission Mode",
        "type": "choice",
        "choices": ["ask", "whitelist", "allow_except_rm", "allow_all"],
        "help": "ask — approve each command\nwhitelist — only safe commands\nallow_except_rm — all except rm\nallow_all — no approval needed",
    },
    {
        "path": "agent.approval_location",
        "label": "Approval Location",
        "type": "choice",
        "choices": ["chat", "cmd"],
        "help": "chat — prompt in chat panel\ncmd — prompt in cmd panel",
    },
    {
        "path": "agent.auto_continue",
        "label": "Auto Continue",
        "type": "bool",
        "help": "Automatically continue loop after each command",
    },
    {
        "path": "agent.max_history_messages",
        "label": "Max History Messages",
        "type": "int",
        "help": "Max messages in session history",
    },
    {
        "path": "agent.max_history_chars",
        "label": "Max History Chars",
        "type": "int",
        "help": "Max characters in history (for trim)",
    },
    {
        "path": "agent.session_file",
        "label": "Session File",
        "type": "string",
        "help": "Path to session file",
    },
    {
        "path": "agent.fresh_start",
        "label": "Fresh Start",
        "type": "bool",
        "help": "Clear session on each launch",
    },
    # Context
    {
        "path": "context.enabled",
        "label": "Context Rotation Enabled",
        "type": "bool",
        "help": "Enable automatic context rotation",
    },
    {
        "path": "context.threshold",
        "label": "Compaction Threshold (%)",
        "type": "float",
        "help": "Context fill threshold for rotation (1-100)",
    },
    {
        "path": "context.summary_max_tokens",
        "label": "Summary Max Tokens",
        "type": "int",
        "help": "Max tokens for summary generation",
    },
    {
        "path": "context.recent_messages_keep",
        "label": "Recent Messages to Keep",
        "type": "int",
        "help": "Number of recent messages to keep verbatim after rotation",
    },
    {
        "path": "context.archive_dir",
        "label": "Archive Directory",
        "type": "string",
        "help": "Directory for session archives and summaries",
    },
]


# ── Helpers ──────────────────────────────────────────────────

def get_value(config: dict, path: str):
    """Get nested value by dot path (e.g. 'colibri.base_url')."""
    keys = path.split(".")
    val = config
    for k in keys:
        if not isinstance(val, dict) or k not in val:
            return None
        val = val[k]
    return val


def set_value(config: dict, path: str, value):
    """Set nested value by dot path."""
    keys = path.split(".")
    val = config
    for k in keys[:-1]:
        if k not in val:
            val[k] = {}
        val = val[k]
    val[keys[-1]] = value


def parse_value(raw: str, field_type: str):
    """Parse user input string into the correct type."""
    raw = raw.strip()
    if field_type == "int":
        return int(raw)
    if field_type == "float":
        return float(raw)
    if field_type == "bool":
        if raw.lower() in ("true", "1", "yes", "y", "on"):
            return True
        if raw.lower() in ("false", "0", "no", "n", "off"):
            return False
        raise ValueError("Use: true/false, yes/no, y/n")
    if field_type == "int_or_null":
        if raw.lower() in ("null", "none", ""):
            return None
        return int(raw)
    if field_type == "choice":
        return raw
    return raw  # string


def format_value(val) -> str:
    """Format value for display."""
    if val is None:
        return "null"
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, str):
        return f'"{val}"'
    return str(val)


# ── UI ────────────────────────────────────────────────────────

def print_header(title: str):
    width = 50
    print()
    print("┌" + "─" * (width - 2) + "┐")
    print(f"│ {title:<{width - 4}} │")
    print("├" + "─" * (width - 2) + "┤")


def print_footer():
    print("└" + "─" * 48 + "┘")


def show_menu(config: dict):
    """Show all fields with current values."""
    print_header("Colibri Mini Agent — Config Editor")
    print()
    for i, field in enumerate(FIELDS, 1):
        val = get_value(config, field["path"])
        val_str = format_value(val)

        # Truncate long values
        if len(val_str) > 40:
            val_str = val_str[:37] + "..."

        # Choice fields — show options count
        if field["type"] == "choice":
            choices_str = " | ".join(field["choices"])
            if len(choices_str) > 30:
                choices_str = choices_str[:27] + "..."
            print(f"  {i:>2}. {field['label']:<28} {val_str:<12} [{choices_str}]")
        else:
            print(f"  {i:>2}. {field['label']:<28} {val_str}")
    print()
    print("   s. Save & exit")
    print("   q. Quit without saving")
    print("   h. Help for field number")
    print_footer()


def show_help(field: dict):
    """Show help text for a field."""
    print()
    print(f"  ── {field['label']} ──")
    print(f"  Path: {field['path']}")
    print(f"  Type: {field['type']}")
    if field["type"] == "choice":
        print(f"  Options: {', '.join(field['choices'])}")
    print(f"  {field['help']}")
    print()


def edit_field(config: dict, field: dict):
    """Edit a single field interactively."""
    current = get_value(config, field["path"])

    print()
    print(f"  ── {field['label']} ──")
    print(f"  {field['help']}")

    if field["type"] == "choice":
        print("  Options:")
        for j, choice in enumerate(field["choices"], 1):
            marker = " ← current" if choice == current else ""
            print(f"    {j}. {choice}{marker}")
        print()
        raw = input(f"  Choose (1-{len(field['choices'])}) or type value: ").strip()
        if not raw:
            print("  (unchanged)")
            return
        if raw.isdigit() and 1 <= int(raw) <= len(field["choices"]):
            value = field["choices"][int(raw) - 1]
        else:
            if raw in field["choices"]:
                value = raw
            else:
                print(f"  ⚠️ '{raw}' is not a valid choice")
                return
    else:
        print(f"  Current: {format_value(current)}")
        print()
        raw = input("  New value (Enter to keep): ").strip()
        if not raw:
            print("  (unchanged)")
            return
        try:
            value = parse_value(raw, field["type"])
        except ValueError as exc:
            print(f"  ⚠️ {exc}")
            return

    set_value(config, field["path"], value)
    print(f"  ✅ {field['label']} → {format_value(value)}")


def main():
    config_path = Path(__file__).resolve().parent / "config.json"

    # Allow --config arg
    args = sys.argv[1:]
    if args:
        if "--config" in args:
            idx = args.index("--config")
            if idx + 1 < len(args):
                config_path = Path(args[idx + 1]).resolve()
        elif args[0] == "--help" or args[0] == "-h":
            print("Usage: python3 config_editor.py [--config /path/to/config.json]")
            return

    if not config_path.exists():
        print(f"❌ Config not found: {config_path}")
        sys.exit(1)

    config = json.loads(config_path.read_text(encoding="utf-8"))

    print()
    print("  ╔══════════════════════════════════════╗")
    print("  ║   Colibri Mini Agent — Config Editor  ║")
    print("  ╚══════════════════════════════════════╝")

    while True:
        show_menu(config)
        try:
            choice = input("\n  → ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            print("  Quit without saving.")
            break

        if choice == "q":
            print("  Quit without saving.")
            break

        if choice == "s":
            # Save
            tmp = config_path.with_name(config_path.name + ".tmp")
            tmp.write_text(
                json.dumps(config, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            import os
            os.replace(tmp, config_path)
            print(f"  ✅ Saved to {config_path}")
            break

        if choice == "h":
            try:
                num = int(input("  Field number: ").strip())
                if 1 <= num <= len(FIELDS):
                    show_help(FIELDS[num - 1])
                else:
                    print("  ⚠️ Invalid field number")
            except ValueError:
                print("  ⚠️ Enter a number")
            continue

        # Numeric — edit field
        try:
            num = int(choice)
        except ValueError:
            print("  ⚠️ Unknown command")
            continue

        if 1 <= num <= len(FIELDS):
            edit_field(config, FIELDS[num - 1])
        else:
            print("  ⚠️ Invalid field number")


if __name__ == "__main__":
    main()
