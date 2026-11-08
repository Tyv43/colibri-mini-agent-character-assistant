#!/usr/bin/env python3
"""Test agent logic without Colibri and without curses/tmux."""

import sys
import threading
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from core.agent import Agent, AgentState
from core.colibri_client import ColibriClient
from core.session import Session
from utils.commands import is_whitelisted, contains_rm


class MockColibri(ColibriClient):
    def __init__(self, responses):
        super().__init__("http://localhost:1", "key", "mock", None)
        self.responses = responses
        self._idx = 0

    def chat_stream(self, messages, tools=None, timeout=30):
        if self._idx >= len(self.responses):
            yield {"type": "done"}
            return
        resp = self.responses[self._idx]
        self._idx += 1
        text = resp.get("text", "")
        if resp.get("cmd"):
            text += f"\n```cmd\n{resp['cmd']}\n```"
        if text:
            yield {"type": "text", "content": text}
        yield {"type": "done"}


def _wait_state(agent, target, timeout=5):
    """Wait until agent.state == target."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if agent.state == target:
            return True
        time.sleep(0.05)
    return False


def make_agent(responses, workspace=None, **kwargs):
    ws = workspace or (Path(__file__).parent / f"test_ws_{uuid.uuid4().hex[:6]}")
    ws.mkdir(parents=True, exist_ok=True)
    colibri = MockColibri(responses)
    session = Session("test system prompt", None, 40, 120000)
    agent = Agent(
        colibri=colibri,
        system_prompt="test system prompt",
        tools=None,
        workspace=ws,
        session=session,
        **kwargs,
    )
    return agent


def test_basic_approve():
    responses = [{"cmd": "echo hello"}]
    agent = make_agent(responses, permission_mode="ask")
    agent.send_message("test")
    assert _wait_state(agent, AgentState.WAITING_APPROVAL), f"state={agent.state}"
    agent.approve(True)
    assert _wait_state(agent, AgentState.WAITING_DECISION), f"state={agent.state}"
    assert agent._last_result["exit_code"] == 0
    assert "hello" in agent._last_result["stdout"]
    agent.done()
    assert agent.state == AgentState.IDLE
    agent.close()
    print("✅ test_basic_approve")


def test_reject():
    responses = [{"cmd": "echo hello"}]
    agent = make_agent(responses, permission_mode="ask")
    agent.send_message("test")
    assert _wait_state(agent, AgentState.WAITING_APPROVAL)
    agent.approve(False)
    assert _wait_state(agent, AgentState.IDLE), f"state={agent.state}"
    user_msgs = [m for m in agent.session.history if m.get("role") == "user"]
    assert any("rejected" in m.get("content", "").lower() for m in user_msgs)
    agent.close()
    print("✅ test_reject")


def test_whitelist_mode():
    responses = [{"cmd": "ls"}]
    agent = make_agent(responses, permission_mode="whitelist")
    agent.send_message("test")
    assert _wait_state(agent, AgentState.WAITING_DECISION), f"state={agent.state}"
    agent.done()
    assert agent.state == AgentState.IDLE
    agent.close()
    print("✅ test_whitelist_mode")


def test_auto_continue():
    responses = [
        {"cmd": "echo step1"},
        {"cmd": "echo step2"},
        {"text": "Done!"},
    ]
    agent = make_agent(responses, permission_mode="allow_all", auto_continue=True)
    agent.send_message("test")
    assert _wait_state(agent, AgentState.IDLE, timeout=10), f"state={agent.state}"
    assert agent._commands_run == 2
    agent.close()
    print("✅ test_auto_continue")


def test_max_steps():
    responses = [
        {"cmd": "echo 1"},
        {"cmd": "echo 2"},
        {"cmd": "echo 3"},
        {"text": "Limit reached. Here's what I have."},
    ]
    agent = make_agent(responses, permission_mode="allow_all", auto_continue=True, max_steps=2)
    agent.send_message("test")
    assert _wait_state(agent, AgentState.IDLE, timeout=10), f"state={agent.state}"
    assert agent._commands_run == 2
    agent.close()
    print("✅ test_max_steps")


def test_duplicate():
    responses = [
        {"cmd": "echo dup"},
        {"cmd": "echo dup"},
    ]
    agent = make_agent(responses, permission_mode="allow_all", auto_continue=True)
    agent.send_message("test")
    assert _wait_state(agent, AgentState.IDLE, timeout=10), f"state={agent.state}"
    assert agent._commands_run == 1
    agent.close()
    print("✅ test_duplicate")


def test_stop_during_command():
    responses = [{"cmd": "sleep 10"}]
    agent = make_agent(responses, permission_mode="allow_all", command_timeout=60)
    t = threading.Thread(target=agent.send_message, args=("test",))
    t.start()
    time.sleep(0.5)
    assert agent.state == AgentState.RUNNING_COMMAND, f"state={agent.state}"
    agent.stop()
    t.join(timeout=10)
    assert agent.state == AgentState.IDLE, f"state={agent.state}"
    agent.close()
    print("✅ test_stop_during_command")


def test_done():
    responses = [{"cmd": "echo test"}]
    agent = make_agent(responses, permission_mode="allow_all")
    agent.send_message("test")
    assert _wait_state(agent, AgentState.WAITING_DECISION), f"state={agent.state}"
    agent.done()
    assert agent.state == AgentState.IDLE
    assert agent._last_result is None
    agent.close()
    print("✅ test_done")


def test_reset():
    responses = [{"cmd": "echo test"}]
    agent = make_agent(responses, permission_mode="allow_all")
    agent.send_message("test")
    assert _wait_state(agent, AgentState.WAITING_DECISION)
    agent.reset()
    assert agent._commands_run == 0
    assert len(agent.session.history) > 1
    agent.close()
    print("✅ test_reset")


def test_new_session():
    responses = [{"cmd": "echo test"}]
    agent = make_agent(responses, permission_mode="allow_all")
    agent.send_message("test")
    agent.new_session()
    assert len(agent.session.history) == 1
    agent.close()
    print("✅ test_new_session")


def test_set_mode_validation():
    agent = make_agent([])
    try:
        agent.set_mode("invalid")
        assert False, "should raise"
    except ValueError:
        pass
    agent.set_mode("ask")
    agent.set_mode("whitelist")
    agent.set_mode("allow_except_rm")
    agent.set_mode("allow_all")
    agent.close()
    print("✅ test_set_mode_validation")


def test_whitelist():
    assert is_whitelisted("ls") == True
    assert is_whitelisted("ls -la") == True
    assert is_whitelisted("cat file.txt") == True
    assert is_whitelisted("rm file") == False
    assert is_whitelisted("ls | grep foo") == False
    assert is_whitelisted("ls && rm file") == False
    assert is_whitelisted("rm -rf /") == False
    assert contains_rm("rm file") == True
    assert contains_rm("ls -la") == False
    print("✅ test_whitelist")


def test_fifo_approve():
    responses = [{"cmd": "echo fifo_test"}]
    agent = make_agent(responses, permission_mode="ask")
    agent.send_message("test")
    assert _wait_state(agent, AgentState.WAITING_APPROVAL)
    time.sleep(0.2)
    with open(agent.fifo_path, "w") as f:
        f.write("approve\n")
        f.flush()
    assert _wait_state(agent, AgentState.WAITING_DECISION, timeout=15), f"state={agent.state}"
    assert "fifo_test" in agent._last_result["stdout"]
    agent.done()
    agent.close()
    print("✅ test_fifo_approve")


def main():
    tests = [
        test_basic_approve,
        test_reject,
        test_whitelist_mode,
        test_auto_continue,
        test_max_steps,
        test_duplicate,
        test_stop_during_command,
        test_done,
        test_reset,
        test_new_session,
        test_set_mode_validation,
        test_whitelist,
        test_fifo_approve,
    ]
    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            passed += 1
        except Exception as exc:
            print(f"❌ {test.__name__}: {exc}")
            import traceback
            traceback.print_exc()
            failed += 1
    print(f"\n{'='*40}")
    print(f"Passed: {passed}/{len(tests)}")
    if failed:
        print(f"Failed: {failed}")
        sys.exit(1)


if __name__ == "__main__":
    main()