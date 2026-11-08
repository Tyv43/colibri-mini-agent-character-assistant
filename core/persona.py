"""Persona loader: assemble system prompt from directive + character card.

Files in persona/ directory:
    directive.md         — roleplay rules and behavior
    character.json       — chara_card_v2 format
    context_template.md  — template for context restore after rotation

The system prompt is built as:
    [directive]
    — Character card: —
    [name, description, personality, scenario, mes_example]
    [context block or empty for new session]
"""

import json
from pathlib import Path


def load_persona(persona_dir: Path) -> dict:
    """Load persona files from directory.

    Returns dict with:
        directive: str
        character: dict (parsed card data)
        context_template: str
        name: str (character name)
        first_mes: str (first message)
    """
    persona_dir = Path(persona_dir)

    # Character card
    char_path = persona_dir / "character.json"
    name = ""
    first_mes = ""
    description = ""
    personality = ""
    scenario = ""
    mes_example = ""

    if char_path.exists():
        raw = json.loads(char_path.read_text(encoding="utf-8"))
        data = raw.get("data", raw)
        name = data.get("name", "")
        description = data.get("description", "")
        personality = data.get("personality", "")
        first_mes = data.get("first_mes", "")
        scenario = data.get("scenario", "")
        mes_example = data.get("mes_example", "")

    # Context template
    template_path = persona_dir / "context_template.md"
    context_template = template_path.read_text(encoding="utf-8") if template_path.exists() else ""

    # Language directive
    lang_path = persona_dir / "language.md"
    language = lang_path.read_text(encoding="utf-8").strip() if lang_path.exists() else ""

    return {
        "name": name,
        "description": description,
        "personality": personality,
        "scenario": scenario,
        "mes_example": mes_example,
        "first_mes": first_mes,
        "context_template": context_template,
        "language": language,
    }


def build_card_block(persona: dict) -> str:
    """Build character card text from persona dict."""
    parts = []
    parts.append("— Character card: —")
    parts.append(f"Name: {persona['name']}")
    if persona["description"]:
        parts.append(f"Description: {persona['description']}")
    if persona["personality"]:
        parts.append(f"Personality: {persona['personality']}")
    if persona["scenario"]:
        parts.append(f"Scenario: {persona['scenario']}")
    if persona["mes_example"]:
        parts.append(f"Dialogue examples:\n{persona['mes_example']}")
    return "\n\n".join(parts)


def _load_directive(persona_dir: Path, mode: str) -> str:
    """Load the right directive file for the mode.

    rp_agent → directive.md (with ACTIONS section)
    chat     → directive_chat.md (pure roleplay, no commands)
    """
    if mode == "chat":
        chat_path = persona_dir / "directive_chat.md"
        if chat_path.exists():
            return chat_path.read_text(encoding="utf-8").strip()
    # Default and rp_agent
    main_path = persona_dir / "directive.md"
    if main_path.exists():
        return main_path.read_text(encoding="utf-8").strip()
    return ""


def build_system_prompt(persona: dict, context_block: str = "", mode: str = "rp_agent", technical_prompt: str = "", persona_dir: Path | None = None) -> str:
    """Assemble full system prompt from persona + optional context block.

    Args:
        persona: result of load_persona()
        context_block: either "" (new session) or context restore text
        mode: "rp_agent" (with commands) or "chat" (pure roleplay, no commands)
        technical_prompt: extra technical instructions (from config prompt.system)
        persona_dir: path to persona/ directory for loading directive files

    Returns:
        Full system prompt string
    """
    sections = []

    # 1. Directive (load the right file for the mode)
    directive = ""
    if persona_dir:
        directive = _load_directive(persona_dir, mode)
    if directive:
        sections.append(directive)

    # 1b. Language directive
    if persona.get("language"):
        sections.append(persona["language"])

    # 2. Character card
    sections.append(build_card_block(persona))

    # 3. Technical instructions (from prompt.system) — only in rp_agent mode
    if mode == "rp_agent" and technical_prompt:
        sections.append(f"[TECHNICAL INSTRUCTIONS]\n{technical_prompt}")
    # 4. Context (new session or restored)
    if context_block:
        sections.append(context_block)
    else:
        sections.append("[New session. Start with first_mes or naturally enter the conversation.]")

    return "\n\n".join(sections)


def build_context_restore(persona: dict, summary: str) -> str:
    """Build context restore block from template + summary.

    Args:
        persona: result of load_persona()
        summary: compressed summary text from context rotation

    Returns:
        Formatted context restore block
    """
    template = persona["context_template"]
    if not template:
        # Fallback if no template
        return (
            f"[CONTEXT RESTORE]\n"
            f"Context was compacted. Use the summary as known context and continue the conversation.\n\n"
            f"{summary}\n[/CONTEXT RESTORE]"
        )
    return template.replace("{SUMMARY}", summary)
