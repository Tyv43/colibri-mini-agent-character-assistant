"""Permission modes for command execution."""

from utils.commands import contains_rm, is_whitelisted

MODES = ("ask", "whitelist", "allow_except_rm", "allow_all")


def needs_approval(command: str, mode: str) -> bool:
    """Check if a command needs user approval based on permission mode."""
    if mode == "allow_all":
        return False
    if mode == "allow_except_rm":
        return contains_rm(command)
    if mode == "whitelist":
        return not is_whitelisted(command)
    return True  # "ask" mode — always approve