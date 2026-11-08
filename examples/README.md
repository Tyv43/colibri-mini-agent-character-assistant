# Example Configurations

Three pre-configured setups for different use cases.

## `config_rp_agent.json` — Roleplay Agent (default)

AI assistant with a character persona + command execution.

| Setting | Value |
|---|---|
| mode | `rp_agent` |
| permission_mode | `ask` |
| approval_location | `chat` |
| auto_continue | `true` |
| max_steps | 4 |
| context.threshold | 75% |
| max_history_messages | 20 |

Use this when you want a character that can also run commands on your system.

## `config_agent.json` — Pure CLI Agent

No persona, no roleplay — just a capable local agent.

| Setting | Value |
|---|---|
| mode | `agent` |
| permission_mode | `allow_except_rm` |
| approval_location | `chat` |
| auto_continue | `true` |
| max_steps | 10 |
| context.threshold | 75% |
| max_history_messages | 20 |

Use this for system administration, automation, or dev work. More commands allowed per turn (10).

## `config_chat.json` — Pure Roleplay Chat

Character persona only, no command execution.

| Setting | Value |
|---|---|
| mode | `chat` |
| permission_mode | `ask` |
| approval_location | `chat` |
| auto_continue | `false` |
| max_steps | 4 (unused) |
| context.threshold | 80% |
| max_history_messages | 30 |
| temperature | 0.8 |

Use this for pure conversation/roleplay. Higher threshold (80%) and more history (30 messages) since no command overhead.

## Usage

```bash
# Use a specific config
python3 agent.py --config examples/config_chat.json

# Or copy one to the project root
cp examples/config_chat.json config.json
python3 agent.py
```