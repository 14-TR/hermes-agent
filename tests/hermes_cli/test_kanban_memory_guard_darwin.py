"""LOCAL PATCH t_b16701f6: macOS memory-pressure source + strict mode + critical alert."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _cfg(monkeypatch, **kw):
    monkeypatch.setattr(kbd, "_kanban_cfg", lambda: kw)


def _darwin(monkeypatch, level, free_pct=50.0):
    monkeypatch.setattr(kbd, "_darwin_pressure_sample",
                        lambda: {"kernel_level": level, "free_pct": free_pct})


def _run(n=3):
    spawns = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 42

    with kbc.connect() as conn:
        for i in range(n):
            kb.create_task(conn, title=f"t{i}", assignee="alice")
        res = kbd.dispatch_once(conn, spawn_fn=fake_spawn)
        alerts = [t for t in kb.list_tasks(conn) if t.title.startswith("ALERT: host memory")]
    return spawns, res, alerts


@pytest.mark.parametrize("sample,cfg,expected", [
    ({"kernel_level": 1, "free_pct": 40}, 15, "ok"),
    ({"kernel_level": 2, "free_pct": 40}, 15, "elevated"),
    ({"kernel_level": 4, "free_pct": 40}, 15, "critical"),
    ({"kernel_level": 1, "free_pct": 10}, 15, "elevated"),
    ({"kernel_level": 1, "free_pct": 10}, 0, "ok"),
    ({"kernel_level": 99, "free_pct": 40}, 15, "unknown"),
])
def test_darwin_level_mapping(sample, cfg, expected):
    assert kbd._darwin_pressure_level(sample, min_free_pct=cfg) == expected


def test_darwin_normal_spawns_all(kanban_home, all_assignees_spawnable, monkeypatch):
    _cfg(monkeypatch, memory_guard_block_on_elevated=True, memory_guard_alert_assignee="big")
    _darwin(monkeypatch, 1)
    spawns, res, alerts = _run()
    assert len(spawns) == 3 and res.memory_pressure is None and not alerts


def test_darwin_warn_strict_spawns_nothing(kanban_home, all_assignees_spawnable, monkeypatch):
    _cfg(monkeypatch, memory_guard_block_on_elevated=True, memory_guard_alert_assignee="big")
    _darwin(monkeypatch, 2)
    spawns, res, alerts = _run()
    assert not spawns and res.memory_pressure == "elevated" and not alerts


def test_darwin_warn_default_mode_spawns_one(kanban_home, all_assignees_spawnable, monkeypatch):
    _cfg(monkeypatch)
    _darwin(monkeypatch, 2)
    spawns, res, _ = _run()
    assert len(spawns) == 1 and res.memory_pressure == "elevated"


def test_darwin_low_free_pct_strict_spawns_nothing(kanban_home, all_assignees_spawnable, monkeypatch):
    _cfg(monkeypatch, memory_guard_block_on_elevated=True, memory_guard_min_free_pct=15)
    _darwin(monkeypatch, 1, free_pct=8.0)
    spawns, res, _ = _run()
    assert not spawns and res.memory_pressure == "elevated"


def test_darwin_critical_spawns_nothing_and_alerts_once(kanban_home, all_assignees_spawnable, monkeypatch):
    _cfg(monkeypatch, memory_guard_block_on_elevated=True, memory_guard_alert_assignee="big")
    _darwin(monkeypatch, 4)
    spawns, res, alerts = _run()
    assert not spawns and res.memory_pressure == "critical"
    assert len(alerts) == 1 and alerts[0].assignee == "big" and alerts[0].status == "blocked"
    # second tick in same hour: deduped, alert itself not spawned
    spawns2, _, alerts2 = _run(n=0)
    assert not spawns2 and len(alerts2) == 1


def test_critical_without_alert_assignee_posts_nothing(kanban_home, all_assignees_spawnable, monkeypatch):
    _cfg(monkeypatch)
    _darwin(monkeypatch, 4)
    spawns, _res, alerts = _run()
    assert not spawns and not alerts


def test_linux_sample_takes_precedence(monkeypatch):
    monkeypatch.setattr(kbd, "_darwin_pressure_sample", lambda: {"kernel_level": 4, "free_pct": 1})
    assert kbd._memory_pressure_level({"mem_available_kib": 10**8, "mem_total_kib": 2 * 10**8}) == "ok"

