"""Utilities: whitelist, terminate, tail."""

import os
import re
import shlex
import signal

WHITELISTED_COMMANDS = {
    "pwd", "ls", "cat", "head", "tail", "wc", "file", "stat", "du", "df",
    "free", "echo", "printf", "date", "whoami", "hostname", "uname", "which",
    "find", "grep", "rg", "sort", "uniq", "cut", "top", "htop", "ps",
    "uptime", "lscpu", "lspci", "lsusb", "lsblk", "mount", "env",
    "ip", "ifconfig", "netstat", "ss", "dig", "nslookup", "ping",
    "python3", "python", "node", "jq", "awk", "sed",
}

SHELL_OPERATORS = (";", "&&", "||", ">", ">>", "<", "`", "$(", "\n", "\r")


def is_whitelisted(command: str) -> bool:
    """Check if a command is safe to run without approval."""
    if any(tok in command for tok in SHELL_OPERATORS):
        return False
    try:
        parts = shlex.split(command, posix=True)
    except ValueError:
        return False
    if not parts:
        return False
    executable = os.path.basename(parts[0])
    if executable not in WHITELISTED_COMMANDS:
        return False
    for arg in parts[1:]:
        path_part = arg.split("=", 1)[-1]
        if (path_part.startswith("/")
                or path_part == ".."
                or path_part.startswith("../")
                or "/../" in path_part):
            return False
    if executable == "find":
        dangerous = ("-exec", "-execdir", "-delete", "-ok", "-okdir")
        if any(a in parts[1:] for a in dangerous):
            return False
    return True


def contains_rm(command: str) -> bool:
    """Check if a command contains 'rm'."""
    return bool(re.search(r"(?<![\w-])rm(?![\w-])", command))


def terminate_process(process):
    """Terminate a subprocess gracefully, then forcefully if needed."""
    if process is None or process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=3)
    except Exception:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except Exception:
            pass
        try:
            process.wait(timeout=3)
        except Exception:
            pass


def tail_text(handle, limit: int = 16384) -> str:
    """Read the tail end of a file handle."""
    handle.flush()
    size = os.fstat(handle.fileno()).st_size
    handle.seek(max(0, size - limit))
    text = handle.read().decode("utf-8", errors="replace")
    if size > limit:
        text = "[output truncated]\n" + text
    return text