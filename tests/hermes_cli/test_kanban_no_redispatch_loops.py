"""t_72f980ce: BIG-assigned cards and block-loop escalations must not re-dispatch.

Reproduces the t_ec48f604 loop: a card assigned to ``big`` was spawned, BIG's
guard refused kanban_complete, the card blocked until block_loop_detected sent
it to triage, and the triage sweep re-promoted it ~90s later (18 runs).
"""
from __future__ import annotations

from pathlib import Path

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_decompose, kanban_specify


def _home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)


def test_big_assigned_card_is_never_spawned(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    spawned = []
    with kbc.connect() as conn:
        big = kb.create_task(conn, title="big card", assignee="big")
        ok = kb.create_task(conn, title="builder card", assignee="builder")
        for _ in range(3):
            res = kbd.dispatch_once(conn, spawn_fn=lambda t, ws: spawned.append(t.id) or 4242)
        assert kb.get_task(conn, big).status == "ready"
        kinds = [e.kind for e in kb.list_events(conn, big)]
    assert big not in spawned and ok in spawned
    assert big in res.skipped_nonspawnable
    assert kinds.count("skipped_nonspawnable") == 1


def test_big_is_not_usable_as_default_assignee(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    assert kbd._resolve_default_assignee("big") is None


def test_block_loop_card_stays_parked_in_triage(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="loops", assignee="builder")
        for _ in range(kb.BLOCK_RECURRENCE_LIMIT):
            if kb.get_task(conn, tid).status == "blocked":
                kb.unblock_task(conn, tid)
            assert kb.block_task(conn, tid, reason="cannot close", kind="needs_input")
        task = kb.get_task(conn, tid)
        assert task.status == "triage"
        assert kb.list_events(conn, tid)[-1].kind == "block_loop_detected"
    # Neither triage sweep picks it up, so nothing re-promotes it.
    assert tid not in kanban_decompose.list_triage_ids()
    assert tid not in kanban_specify.list_triage_ids()
    spawned = []
    with kbc.connect() as conn:
        kbd.dispatch_once(conn, spawn_fn=lambda t, ws: spawned.append(t.id) or 4242)
        assert kb.get_task(conn, tid).status == "triage"
    assert spawned == []


def test_fresh_triage_card_still_listed(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="fresh", assignee="builder", triage=True)
    assert tid in kanban_specify.list_triage_ids()
    assert tid in kanban_decompose.list_triage_ids()
