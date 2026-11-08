"""Triage release: ``hermes kanban unblock`` returns a block-loop triage card to work (t_82f5944d)."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from hermes_cli import kanban as kanban_cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _run(conn, tid):
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert kb.claim_task(conn, tid, claimer="worker") is not None


def _looped(conn, parents=()):
    tid = kb.create_task(conn, title="loop", assignee="worker")
    for p in parents:
        kb.link_tasks(conn, parent_id=p, child_id=tid)
    _run(conn, tid)
    kb.block_task(conn, tid, reason="x", kind="capability")
    kb.unblock_task(conn, tid)
    _run(conn, tid)
    kb.block_task(conn, tid, reason="x", kind="capability")
    assert kb.get_task(conn, tid).status == "triage"
    return tid


def _unblock(tid, reason=None):
    return kanban_cli._cmd_unblock(argparse.Namespace(task_ids=[tid], ids=None, task_id=None, reason=reason))


def test_release_to_ready_resets_loop_and_records_event(kanban_home):
    with kbc.connect_closing() as conn:
        tid = _looped(conn)
        assert kb.release_triage_task(conn, tid)
        t = kb.get_task(conn, tid)
        assert t.status == "ready"
        assert t.block_recurrences == 0
        ev = [e for e in kb.list_events(conn, tid) if e.kind == "unblocked"][-1]
        assert ev.payload["from_status"] == "triage"
        assert ev.payload["block_loop_reset"] is True
        assert ev.payload["block_loop_detected"] is True
        assert ev.payload["prev_block_recurrences"] == 2
        # One more same-kind block is a fresh first block, not instant triage.
        _run(conn, tid)
        kb.block_task(conn, tid, reason="x", kind="capability")
        assert kb.get_task(conn, tid).status == "blocked"


def test_release_lands_in_todo_with_open_parent(kanban_home):
    with kbc.connect_closing() as conn:
        parent = kb.create_task(conn, title="p", assignee="worker")
        tid = _looped(conn)
        kb.link_tasks(conn, parent_id=parent, child_id=tid)
        assert kb.release_triage_task(conn, tid)
        assert kb.get_task(conn, tid).status == "todo"


def test_release_refuses_non_triage(kanban_home):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="r", assignee="worker")
        assert not kb.release_triage_task(conn, tid)


def test_cli_unblock_releases_triage_with_comment(kanban_home, capsys):
    with kbc.connect_closing() as conn:
        tid = _looped(conn)
    assert _unblock(tid, reason="human fixed creds") == 0
    assert f"Unblocked {tid}" in capsys.readouterr().out
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "ready"
        assert any("human fixed creds" in c.body for c in kb.list_comments(conn, tid))


def test_run_slash_unblock_end_to_end(kanban_home):
    """Same argv entry point the terminal CLI and gateway /kanban use."""
    with kbc.connect_closing() as conn:
        parent = kb.create_task(conn, title="p", assignee="worker")
        tid = _looped(conn)
        kb.link_tasks(conn, parent_id=parent, child_id=tid)
    out = kanban_cli.run_slash(f'unblock --reason "loop fixed" {tid}')
    assert f"Unblocked {tid}" in out
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "todo"
    # Second call is a clean refusal, not a crash.
    assert "cannot unblock" in kanban_cli.run_slash(f"unblock {tid}")
