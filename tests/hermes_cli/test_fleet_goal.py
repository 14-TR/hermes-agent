"""Fleet goal-mode switch (t_5c87cbd3): Size M/L cards become goal-mode while the flag is on."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import fleet_goal as fg
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / ".hermes"
    h.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(h))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_FLEET_GOAL_FILE", str(tmp_path / "state" / "fleet_goal.json"))
    kb.init_db()
    return h


def _create(body):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="demo", body=body, assignee="builder")
        return kb.get_task(conn, tid)


def test_off_by_default_leaves_cards_single_shot(home):
    t = _create("Size: M")
    assert not t.goal_mode and t.goal_max_turns is None


@pytest.mark.parametrize("body,mode,turns", [
    ("Objective: x\nSize: M", True, 30),
    ("Due: 2026-10-27. Size: L.", True, 60),  # BIG card style
    ("Size: L", True, 60),
    ("**Size:** M", True, 30),
    ("Size: S", False, None),
    ("no size here", False, None),
    ("Size: M\nGoal: off", False, None),
])
def test_on_applies_by_size(home, body, mode, turns):
    fg.save(True)
    t = _create(body)
    assert (bool(t.goal_mode), t.goal_max_turns) == (mode, turns)


def test_explicit_caller_choice_wins(home):
    fg.save(True)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="x", body="Size: L", goal_max_turns=5)
        assert kb.get_task(conn, tid).goal_max_turns == 5


def test_toggle_and_status(home):
    assert "ON" in fg.command("on")
    assert json.loads(fg.flag_path().read_text())["enabled"] is True
    _create("Size: M")
    assert "in flight: 1" in fg.command("status")
    assert "OFF" in fg.command("off")
    assert not _create("Size: M").goal_mode
    assert fg.command("bogus").startswith("Usage")


def test_corrupt_flag_never_breaks_create(home):
    fg.flag_path().parent.mkdir(parents=True, exist_ok=True)
    fg.flag_path().write_text("{nope")
    assert not _create("Size: M").goal_mode
