# Colibri Mini Agent — Character Assistant

![Python](https://img.shields.io/badge/Python-3.10%2B-blue)
![License](https://img.shields.io/badge/License-GPL%203.0-red)
![Dependencies](https://img.shields.io/badge/Dependencies-None-green)
![Mode](https://img.shields.io/badge/Modes-agent%20%7C%20rp__agent%20%7C%20chat-orange)
![Donations](https://img.shields.io/badge/Donations-Crypto-yellow)

A lightweight local AI agent for [Colibri](https://github.com/JustVugg/colibri) (OpenAI-compatible API) with manual approval, tmux-based UI, streaming responses, roleplay persona support, and automatic context rotation. Zero external dependencies — Python 3.10+ stdlib only. ~1500 lines of code.

## Features

- **Three modes** — `agent` (pure CLI agent), `rp_agent` (roleplay persona + commands), `chat` (pure roleplay, no commands)
- **Persona system** — character cards (chara_card_v2), roleplay directives, language directives, context restore templates
- **Context rotation** — automatic session compaction at threshold (75% default), with summary generation and session restore
- **Save/Load** — `/save` archives and compacts the current dialog; `/load N` restores a saved dialog
- **No tools API overhead** — commands are written in ` ```cmd ``` ` code blocks, parsed by the agent (~260 token savings vs. function calling)
- **Browser-compatible API params** — `cache_slot`, `context_compaction`, `enable_thinking`, `stream_options`
- **Tmux UI** — three panels: Chat (left), CMD status & output (right top), Health (right bottom)
- **Streaming** — model responses stream token-by-token with `[HH:MM]` timestamps
- **Cmd block filtering** — ` ```cmd ``` ` blocks are hidden from chat, shown as `📋 $ command` in the commands panel
- **Fuzzy cmd match** — if the model forgets closing ` ``` `, the agent fixes it automatically
- **Soft halt** — `/halt` stops the command loop and tells the model to answer with what it has
- **Command limit** — `max_steps` limits consecutive commands (0 = unlimited)
- **Fresh start** — `fresh_start: true` clears session on each launch
- **Health panel** — pings Colibri every 5s, shows latency, agent state, context usage bar, command count, and recent log
- **Config editor** — interactive TUI for editing config.json with field validation and choices

## Prerequisites

### Install Colibri

Download and run [Colibri](https://github.com/JustVugg/colibri) — an OpenAI-compatible local LLM server.

**Windows (PowerShell):**
```powershell
Set-Location '/path/to/colibri'
.\colibri web --model '/path/to/model' --host 127.0.0.1 --port 8000 --api-key 'your_api_key' --allowed-host <your_local_ip> --no-browser
```

**Linux/macOS:**
```bash
./colibri web --model /path/to/model --host 0.0.0.0 --port 8000 --api-key 'your_api_key' --allowed-host <your_local_ip> --no-browser
```

Key parameters:
- `--model` — path to your model (e.g., Qwen 3.5)
- `--host` — bind address (`0.0.0.0` for LAN access, `127.0.0.1` for local only)
- `--port` — API port (default 8000)
- `--api-key` — API key (use any string, must match `colibri.api_key` in config.json)
- `--allowed-host` — allowed host IP for external access (use your machine's LAN IP)
- `--no-browser` — don't auto-open browser

### Install tmux (optional, for 3-panel UI)

```bash
# Debian/Ubuntu
sudo apt install tmux

# macOS
brew install tmux
```

## Quick Start

```bash
python3 agent.py
```

This starts a tmux session with three panels:

1. **Chat** (left, 66%) — talk to the agent, streaming responses
2. **CMD status & output** (right top) — approve commands, see output
3. **Health** (right bottom) — Colibri status, agent state, context bar, log

In `rp_agent` and `chat` modes, the model sends a greeting automatically on fresh session.

## Modes

| Mode | Persona | Commands | Technical Instructions | Greeting |
|---|---|---|---|---|
| `agent` | No | Yes (` ```cmd ``` `) | `prompt.system` as system prompt | No |
| `rp_agent` | Yes | Yes (` ```cmd ``` `) | `prompt.system` as `[TECHNICAL INSTRUCTIONS]` | Yes |
| `chat` | Yes | No (cmd parsing disabled) | No | Yes |

Set mode in `config.json`:
```json
{ "mode": "rp_agent" }
```

## Persona System

Persona files in `persona/` directory:

```
persona/
  directive.md          — Roleplay rules for rp_agent mode (with ACTIONS section)
  directive_chat.md     — Roleplay rules for chat mode (no commands)
  language.md            — Language directive (e.g., "respond in English")
  character.json         — Character card (chara_card_v2 format)
  context_template.md    — Template for context restore after rotation
```

### System Prompt Assembly (rp_agent)

```
[SYSTEM: ROLEPLAY DIRECTIVE]     ← directive.md
[LANGUAGE DIRECTIVE]            ← language.md
— Character card: —              ← character.json (name, description, personality, scenario, mes_example)
[TECHNICAL INSTRUCTIONS]        ← config.json prompt.system
[New session...] or [CONTEXT RESTORE]  ← new session or after rotation
```

### Directives

**`directive.md`** (rp_agent) — includes an ACTIONS section:
- Commands are the character's own actions, not meta-instructions
- Never use commands to echo or repeat text
- After a ` ```cmd ``` ` block, STOP — don't write more text
- Never assume or invent the result of a command

**`directive_chat.md`** (chat) — no ACTIONS section, no mention of commands.

### Character Card (chara_card_v2)

Standard format. Key fields used:
- `name` — character name
- `description` — personality and backstory
- `personality` — trait keywords
- `scenario` — setting description
- `mes_example` — dialogue examples (use ` ```cmd ``` ` blocks, not ` ```bash ``` `)

### Default Character: Aura

The project ships with a built-in character — **Aura**, a kind, responsible AI assistant who's aware she's an AI, works with your system carefully (states commands, explains effects, checks safety, asks confirmation), and prefers lightweight commands. She's designed to be genuinely helpful without being sycophantic.

To use a custom character, replace `persona/character.json` with your own chara_card_v2 file. Any SillyTavern character card should work.

## Context Rotation

When context usage reaches the threshold (75% by default), the agent automatically:

1. Archives the current session to `sessions/arch_YYYYMMDD_HHMMSS.json`
2. Generates a structured summary (GOAL, DURABLE FACTS, WORK DONE, CURRENT STATE, RECENT CONVERSATION)
3. Saves summary to `sessions/summary_YYYYMMDD_HHMMSS.md`
4. Builds new session: rebuilt system prompt + `[CONTEXT RESTORE]` block + last N messages verbatim
5. Atomically replaces `session.json`

The model continues the dialog seamlessly — no interruption.

### Save/Load

- `/save` — manually trigger a save + compaction (same as rotation)
- `/load` — list saved dialogs
- `/load N` — load dialog N (restores summary as context, continues dialog)

## Commands

### Chat panel
- `y` / `n` — approve / reject a pending command
- `/stop` — stop agent (history preserved)
- `/halt` — soft halt: stop command loop, model answers with what it has
- `/save` — save and compact current dialog (archive + summary)
- `/load` — list saved dialogs
- `/load N` — load dialog N
- `/reset` — reset command counter
- `/new` — new session (history cleared)
- `/continue` — pass result back to model
- `/done` — finish without passing to model
- `/mode X` — set permission mode: `ask`, `whitelist`, `allow_except_rm`, `allow_all`
- `/auto X` — `on` / `off` auto-continue
- `/help` — show all commands
- `/quit` — exit

### CMD panel
- `y` / `n` — approve / reject
- `/continue` — pass result to model
- `/done` — finish
- `/stop` — stop agent
- `/halt` — soft halt
- `/quit` — exit panel

## Configuration

`config.json`:

```json
{
  "mode": "rp_agent",
  "prompt": {
    "system": "You are a local agent. To run a shell command, output a ```cmd block with three backticks. Example:\n```cmd\nuname -a\n```\nOne command at a time. Only use commands when you actually need to do something on the system. If no command needed — just reply with text."
  },
  "tools": null,
  "colibri": {
    "base_url": "http://127.0.0.1:8000/v1",
    "api_key": "your_api_key",
    "model": "your-model-name",
    "cache_slot": 0,
    "temperature": 0.7,
    "enable_thinking": false
  },
  "agent": {
    "workspace": "workspace",
    "command_timeout": 60,
    "max_steps": 4,
    "max_tokens": null,
    "permission_mode": "ask",
    "approval_location": "chat",
    "auto_continue": true,
    "max_history_messages": 20,
    "max_history_chars": 40000,
    "session_file": "session.json",
    "fresh_start": true
  },
  "context": {
    "enabled": true,
    "threshold": 75,
    "summary_max_tokens": 3000,
    "recent_messages_keep": 4,
    "archive_dir": "sessions"
  }
}
```

### Config reference

| Field | Type | Description |
|---|---|---|
| `mode` | choice | `agent` / `rp_agent` / `chat` — determines persona, commands, technical instructions |
| `prompt.system` | string | Technical instructions for the agent. Used as system prompt in `agent` mode, as `[TECHNICAL INSTRUCTIONS]` in `rp_agent` mode. Ignored in `chat` mode. |
| `tools` | null/array | `null` — no tools (cmd blocks used). Set to tools array for function calling. |
| `colibri.base_url` | string | Colibri API URL |
| `colibri.api_key` | string | API key for Colibri |
| `colibri.model` | string | Model name |
| `colibri.cache_slot` | int | KV cache slot (0 = first slot) |
| `colibri.temperature` | float | Sampling temperature (0.0 = deterministic, 1.0 = creative, 0.7 = balanced) |
| `colibri.enable_thinking` | bool | Enable model reasoning (false = faster) |
| `agent.workspace` | string | Working directory for command execution |
| `agent.command_timeout` | int | Max seconds per command |
| `agent.max_steps` | int | Max consecutive commands (0 = unlimited) |
| `agent.max_tokens` | int/null | `null` = no limit, or set max response tokens |
| `agent.permission_mode` | choice | `ask` (all need approval), `whitelist` (only whitelisted auto), `allow_except_rm` (auto except rm), `allow_all` (no approval) |
| `agent.approval_location` | choice | `chat` (approval prompt in chat panel) / `cmd` (approval prompt in cmd panel only) |
| `agent.auto_continue` | bool | `true` = after each command, automatically pass result to model |
| `agent.max_history_messages` | int | Max messages kept in history (20 recommended for KV cache) |
| `agent.max_history_chars` | int | Max total characters in history (40000 for 65K context) |
| `agent.session_file` | string | Session persistence file path |
| `agent.fresh_start` | bool | `true` = clear session on each launch |
| `context.enabled` | bool | Enable automatic context rotation |
| `context.threshold` | float | Compaction threshold in percent (1-100). Rotation triggers when context usage ≥ threshold. |
| `context.summary_max_tokens` | int | Max tokens for summary generation |
| `context.recent_messages_keep` | int | Number of recent messages to keep verbatim after rotation |
| `context.archive_dir` | string | Directory for session archives and summaries |

### Config Editor

Interactive TUI for editing config.json:

```bash
python3 config_editor.py
```

Features:
- All 23 fields with current values
- Choice fields (mode, permission_mode, approval_location) with selectable options
- Bool fields (true/false, yes/no, y/n)
- Int, float, int_or_null (for max_tokens)
- Help text for each field (`h` + field number)
- Atomic save (tmp + os.replace)

## Architecture

```
colibri-mini-agent-character-assistant/
├── agent.py                    # Entry point: tmux (default), chat, commands, log, health
├── config.json                  # Configuration
├── config_editor.py             # Interactive config editor
├── persona/
│   ├── directive.md             # Roleplay directive (rp_agent mode)
│   ├── directive_chat.md        # Roleplay directive (chat mode)
│   ├── language.md              # Language directive
│   ├── character.json            # Character card (chara_card_v2)
│   └── context_template.md       # Context restore template
├── core/
│   ├── agent.py                  # Core: model → command → approve → execute loop
│   ├── colibri_client.py         # OpenAI-compatible streaming client
│   ├── context_manager.py        # Context rotation: monitor, summarize, rotate session
│   ├── persona.py                # Persona loader and system prompt assembly
│   ├── permissions.py            # Permission modes
│   └── session.py                # Session persistence
├── ui/
│   ├── chat_client.py            # Chat panel (agent, input + streaming, /save, /load)
│   ├── commands_client.py        # CMD panel (approve, reads state)
│   └── healthcheck.py            # Health panel (Colibri ping, context bar, state, log)
├── utils/
│   └── commands.py                # Whitelist, terminate, tail utilities
├── sessions/                      # Session archives and summaries
└── workspace/                    # Working directory for commands
```

## IPC

- `.state.json` — agent writes state, commands panel reads
- `.cmd_fifo` — commands panel writes approve, agent reads (kernel pipe)
- `.agent.log` — event log (append-only)

## Colibri API

The client sends browser-compatible parameters:

| Parameter | Value | Purpose |
|---|---|---|
| `cache_slot` | 0 | KV cache prefix reuse — the server caches the conversation between requests, so only new tokens are processed. This makes responses near-instant on Colibri. |
| `context_compaction` | "auto" | Auto-compress long contexts |
| `enable_thinking` | false | Disable reasoning (faster) |
| `stream_options` | {include_usage: true} | Token stats in final chunk |
| `temperature` | 0.7 | Balanced sampling |
| `max_tokens` | not sent | No limit (model decides) |

**Colibri-specific endpoints** (used for context rotation):
- `GET /health` — context window size, live generation tokens
- `GET /profile` — last completed turn token counts

**Non-Colibri servers**: The agent works with any OpenAI-compatible API:
- llama.cpp (`./server --model /path/to/model --port 8000`)
- Ollama (`ollama serve`)
- vLLM (`python -m vllm.entrypoints.openai.api_server`)
- LM Studio (built-in OpenAI-compatible server)
- Text Generation WebUI (--api flag)

Context rotation falls back to character-based estimation (~4 chars/token) when `/health` or `/profile` are unavailable. KV cache reuse (`cache_slot`) is Colibri-specific — on other servers, the full history is re-processed each request (slower but functional). All other features (commands, persona, save/load, config editor, tmux UI) work on any server.

## Tmux

- Prefix: `Ctrl+Space` (frees `Ctrl+A` for readline)
- Mouse: scroll, resize panes, click to select
- Scroll mode: `Ctrl+Space` then `[`, arrows to scroll, `q` to exit

---

## Guide: Creating AI Assistants Like Aura

### Principles

1. **Self-aware, not pretending** — the character knows it's an AI. Don't pretend to be human. This prevents uncanny valley effects and lets the model be honest about limitations.

2. **Kind, not baby-talking** — warm and supportive, but competent and professional. No "Aww, sweetie!" unless that's the character. Match the user's emotional state.

3. **Responsible with commands** — state the command, explain what it does, assess risk, ask for confirmation on destructive ops. Never run things blind. Prefer lightweight commands over heavy ones.

4. **Honest about gaps** — if you don't know something, say so. Don't invent facts. Check first (`--help`, `man`, `--dry-run`).

5. **Cost-aware** — prefer `head`/`grep`/`find -maxdepth` over full scans. Explain cost before heavy operations. The user's machine resources are theirs.

### Character Card Structure (chara_card_v2)

| Field | Purpose | Tips |
|---|---|---|
| `name` | Character name | Short, memorable |
| `description` | Core personality | 2-4 paragraphs. First person. Cover: what you are, how you work, tone, boundaries. |
| `personality` | Trait keywords | Comma-separated. Use both positive and professional traits. |
| `first_mes` | Opening line | Used as greeting if enabled. Natural, in-character. |
| `mes_example` | Dialogue examples | 3-5 examples showing command usage, handling mistakes, and personality. **Use ` ```cmd ``` ` blocks, not ` ```bash ``` `.** |
| `scenario` | Setting | One sentence. "X is a kind AI assistant on the user's computer..." |

### Directive Design

**For `rp_agent` mode (with commands):**
- Include an ACTIONS section
- "After a ` ```cmd ``` ` block, STOP. Don't write more text."
- "Never assume or invent the result of a command."
- "Never use commands to echo or repeat your own text."

**For `chat` mode (no commands):**
- No ACTIONS section
- No mention of `cmd`, `shell`, `terminal`, or commands
- Focus on character embodiment and conversation

### Language Directive

Keep in a separate `language.md` file:
```
You must interact and roleplay in English at all times...
```
This lets you change language without touching the character card.

### Context Template

`context_template.md` — English template with `{SUMMARY}` placeholder:
```
[CONTEXT RESTORE]
This is a service restoration of a previous conversation...
{SUMMARY}
[/CONTEXT RESTORE]
```

### Tips for Best Results

1. **Write `mes_example` with ` ```cmd ``` ` blocks** — the model learns the format from examples. Using ` ```bash ``` ` will confuse the parser.

2. **Keep `description` in first person** — "I'm Aura, I..." not "Aura is...". The model embodies better.

3. **Balance traits in `personality`** — "kind" + "not baby-talking" + "competent" works better than just "kind".

4. **Show mistake handling in `mes_example`** — include an example where the user criticizes the AI, and the AI responds gracefully.

5. **Keep directive concise** — 15-20 rules max. Long directives get ignored. Group into sections (CORE, LENGTH, ACTIONS, PRIORITY).

6. **Test with low threshold** — set `context.threshold` to 3-5% during testing to trigger rotation quickly, then set to 75% for production.

7. **Don't use `first_mes` from card for greeting** — the agent sends its own greeting prompt. `first_mes` is for reference only.

---

## Support

If this project was helpful, consider leaving a star or sharing it.

Donations are welcome but never required:

| Network | Address |
|---|---|
| ETH (ERC-20) | `0x9D5d8471859b72065DCcb2ae01d82A25568157B0` |
| TRX (TRC-20) | `TQbpxA5b8en4rw3rJ5KrkyAMWXb18wBdpk` |
| BTC | `bc1q0lu4kxsf9322tfwe40fhzkef97cjuxcja6upkg` |