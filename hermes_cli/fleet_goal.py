"""Fleet-wide goal-mode policy (t_5c87cbd3).

A flag file ``<hermes root>/state/fleet_goal.json`` = ``{"enabled": bool, "M": 30, "L": 60}``.
While enabled, :func:`apply_policy` (called from ``kanban_db.create_task``) turns every new card
whose body declares ``Size: M`` or ``Size: L`` into a goal-mode card with that turn budget. ``S``
(or no size) stays single-shot. A caller that already passed ``goal_mode=True`` or an explicit
``goal_max_turns`` is left alone, and a body line ``Goal: off`` opts a single card out.

CLI: ``python -m hermes_cli.fleet_goal on|off|status`` (the BIG ``/fleetgoal`` command wraps it).
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

DEFAULTS = {"enabled": False, "M": 30, "L": 60}
_SIZE_RE = re.compile(r"(?i)\bsize\s*\**\s*[:=]\s*\**\s*([SML])\b")
_OPT_OUT_RE = re.compile(r"(?im)^\W*goal\s*[:=]\s*(off|no|false)\b")


def _root() -> Path:
    override = os.environ.get("HERMES_KANBAN_HOME", "").strip()
    if override:
        return Path(override).expanduser()
    from hermes_constants import get_default_hermes_root
    return get_default_hermes_root()


def flag_path() -> Path:
    override = os.environ.get("HERMES_FLEET_GOAL_FILE", "").strip()
    return Path(override).expanduser() if override else _root() / "state" / "fleet_goal.json"


def load() -> dict:
    """Current flag (defaults when missing or unreadable — never raises)."""
    cfg = dict(DEFAULTS)
    try:
        data = json.loads(flag_path().read_text(encoding="utf-8"))
        if isinstance(data, dict):
            cfg["enabled"] = bool(data.get("enabled", False))
            for k in ("M", "L"):
                v = data.get(k)
                if isinstance(v, int) and v > 0:
                    cfg[k] = v
    except (OSError, ValueError):
        pass
    return cfg


def save(enabled: bool) -> dict:
    cfg = load()
    cfg["enabled"] = bool(enabled)
    cfg["updated_at"] = int(time.time())
    path = flag_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".fleet_goal.")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
    os.replace(tmp, path)
    return cfg


def card_size(body: Optional[str]) -> Optional[str]:
    m = _SIZE_RE.search(body or "")
    return m.group(1).upper() if m else None


def apply_policy(body: Optional[str], goal_mode: bool, goal_max_turns: Optional[int]) -> tuple[bool, Optional[int], bool]:
    """``(goal_mode, goal_max_turns, applied)`` for a card about to be created."""
    if goal_mode or goal_max_turns is not None:
        return goal_mode, goal_max_turns, False
    try:
        cfg = load()
        if not cfg["enabled"] or _OPT_OUT_RE.search(body or ""):
            return goal_mode, goal_max_turns, False
        size = card_size(body)
        if size in ("M", "L"):
            return True, int(cfg[size]), True
    except Exception:  # policy must never break card creation
        pass
    return goal_mode, goal_max_turns, False


def in_flight_goal_cards(db_path: Optional[Path] = None) -> list[tuple[str, str, Optional[int], str]]:
    if db_path is None:
        from hermes_cli.kanban_db import kanban_db_path
        db_path = kanban_db_path()
    db = db_path
    if not Path(db).exists():
        return []
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return con.execute(
            "SELECT id, status, goal_max_turns, title FROM tasks WHERE goal_mode = 1 "
            "AND status IN ('todo','ready','running','review','blocked') ORDER BY created_at"
        ).fetchall()
    finally:
        con.close()


def status_text() -> str:
    cfg = load()
    lines = [f"Fleet goal mode: {'ON' if cfg['enabled'] else 'OFF'} "
             f"(M={cfg['M']} turns, L={cfg['L']} turns, S=single-shot) — {flag_path()}"]
    try:
        rows = in_flight_goal_cards()
        active = [r for r in rows if r[1] != "blocked"]
        lines.append(f"Goal-mode cards in flight: {len(active)} (+{len(rows) - len(active)} blocked)")
        for tid, st, mx, title in rows[:15]:
            lines.append(f"  {tid} {st:<8} max={mx or 'default'}  {title[:60]}")
    except Exception as exc:
        lines.append(f"Goal-mode cards in flight: unknown ({exc})")
    return "\n".join(lines)


def command(raw_args: str = "") -> str:
    arg = (raw_args or "").strip().lower()
    if arg in ("on", "enable", "true", "1"):
        save(True)
    elif arg in ("off", "disable", "false", "0"):
        save(False)
    elif arg not in ("", "status"):
        return "Usage: /fleetgoal on|off|status"
    return status_text()


if __name__ == "__main__":
    print(command(" ".join(sys.argv[1:])))
