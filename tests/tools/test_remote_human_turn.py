"""Remote-human turns (Teams bridge, t_1b19a76f): a phone sender never gets a desktop panel.

Real imports against a temp HERMES_HOME; the approval callback stands in for the desktop panel
and must never be called while the remote marker is bound.
"""
import json

import pytest


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_INTERACTIVE", "1")   # desktop/CLI: a panel WOULD be shown
    monkeypatch.delenv("HERMES_YOLO_MODE", raising=False)
    return home


@pytest.fixture
def remote():
    from tools.approval_context import reset_remote_human_context, set_remote_human_context
    token = set_remote_human_context("teams")
    yield
    reset_remote_human_context(token)


def _panel(calls):
    def callback(*args, **kwargs):
        calls.append(args)
        return "once"
    return callback


def test_dangerous_command_is_denied_with_text_not_a_panel(home, remote):
    from tools.approval import check_all_command_guards
    calls = []
    result = check_all_command_guards("rm -rf /tmp/some-dir", "local", approval_callback=_panel(calls))
    assert calls == []
    assert result["approved"] is False
    assert "teams (phone)" in result["message"] and "approve t_xxxx" in result["message"]


def test_execute_code_is_denied_without_a_panel(home, remote):
    from tools.approval import check_execute_code_guard
    result = check_execute_code_guard("print(1)", "local", has_host_access=True)
    assert result["approved"] is False
    assert "teams (phone)" in result["message"]


def test_without_marker_the_desktop_panel_is_still_used(home, monkeypatch):
    # Hermetic: kanban workers / -q runs export these; the desktop-panel path needs a clean env.
    import os
    for var in [k for k in os.environ if k.startswith("HERMES_") and k != "HERMES_HOME"]:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HERMES_INTERACTIVE", "1")
    from tools.approval import check_all_command_guards
    from tools.approval_context import remote_human_channel
    assert remote_human_channel() == ""
    calls = []
    check_all_command_guards("rm -rf /tmp/some-dir", "local", approval_callback=_panel(calls))
    assert len(calls) == 1


def test_clarify_returns_text_instruction(home, remote):
    from tools.clarify_tool import clarify_tool
    asked = []
    out = json.loads(clarify_tool([{"question": "Proceed?"}], callback=lambda q: asked.append(q)))
    assert asked == []
    assert "phone" in out["error"]


def test_mailbox_round_trips_remote_channel(tmp_path):
    from tools import bot_live_delivery as mailbox
    owner = dict(profile_home=str(tmp_path.resolve()), session_id="chat", lease_id="l", live_session_id="v")
    rec = mailbox.deliver_to_live_owner(tmp_path, owner, "hi", delivery_id="b" * 32, remote_channel="teams")
    assert rec["remote_channel"] == "teams"
    with pytest.raises(ValueError):   # same id, different trust marker: never an overwrite
        mailbox.deliver_to_live_owner(tmp_path, owner, "hi", delivery_id="b" * 32)
    with pytest.raises(ValueError):
        mailbox.deliver_to_live_owner(tmp_path, owner, "hi", remote_channel="Teams; rm")
    assert mailbox.claim_pending_delivery(tmp_path, owner)["remote_channel"] == "teams"
