"""Regression coverage for worker/delegate residue in long-lived dashboard hosts."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace


def _args() -> SimpleNamespace:
    return SimpleNamespace(
        status=False,
        stop=False,
        headless_backend=False,
        ssh_owner_nonce=None,
        ssh_session_token_file=None,
        host="127.0.0.1",
        port=0,
        no_open=True,
        insecure=False,
        open_profile="",
        isolated=True,
        skip_build=True,
    )


def test_dashboard_drops_worker_lineage_before_serving_user_sessions(monkeypatch, tmp_path):
    from hermes_cli import main as cli_main
    from hermes_cli import resource_limits, web_server
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools
    from tools.environments.local import build_subprocess_env

    home = tmp_path / ".hermes"
    home.mkdir()
    workspace = tmp_path / "worker-workspace"
    workspace.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", str(home))
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_parent")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(workspace))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "worker-board.db"))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "worker-board")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "7")
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", "claim")
    monkeypatch.setenv("HERMES_TENANT", "worker-tenant")
    monkeypatch.setenv("HERMES_SESSION_SOURCE", "kanban")
    monkeypatch.setenv("HERMES_PROFILE", "worker-profile")
    monkeypatch.setenv("TERMINAL_CWD", str(workspace))

    monkeypatch.setattr(cli_main, "_attach_to_host_backend", lambda *args: None)
    monkeypatch.setattr(cli_main, "_route_named_profile_dashboard", lambda *args: None)
    monkeypatch.setattr(cli_main, "_dashboard_prepare_runtime", lambda *args: False)
    monkeypatch.setattr(cli_main, "_maybe_setup_dashboard_auth_interactively", lambda *args: None)
    monkeypatch.setattr(resource_limits, "apply_nofile_soft_limit", lambda: None)

    observed: dict[str, dict] = {}

    def fake_start_server(**_kwargs):
        session_env = build_subprocess_env(scrub_secrets=False, inherit_profile_home=True)
        observed["session_env"] = session_env
        assert "HERMES_DELEGATED_CHILD_CONTEXT" not in session_env
        assert not any(key.startswith("HERMES_KANBAN_") for key in session_env)

        kb.init_db()
        created = json.loads(kanban_tools._handle_create({
            "title": "created from dashboard session",
            "assignee": "reviewer",
        }))
        observed["created"] = created

        # Delegated children spawned from the dashboard are still fenced.
        from agent.delegation_context import delegated_child_context
        with delegated_child_context():
            child_env = build_subprocess_env(scrub_secrets=False, inherit_profile_home=True)
            observed["child_env"] = child_env
            observed["child_create"] = json.loads(kanban_tools._handle_create({
                "title": "child should be refused",
                "assignee": "reviewer",
            }))

    monkeypatch.setattr(web_server, "start_server", fake_start_server)

    cli_main.cmd_dashboard(_args())

    assert observed["created"]["ok"] is True
    assert Path(observed["session_env"]["HERMES_HOME"]) == home
    assert "HERMES_TENANT" not in observed["session_env"]
    assert observed["session_env"].get("HERMES_SESSION_SOURCE") != "kanban"
    assert "HERMES_PROFILE" not in observed["session_env"]
    assert "TERMINAL_CWD" not in observed["session_env"]
    assert observed["child_env"].get("HERMES_DELEGATED_CHILD_CONTEXT")
    assert observed["child_create"].get("ok") is not True
    assert "delegate_task child" in json.dumps(observed["child_create"])
