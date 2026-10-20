"""A recycled worker PID is never mistaken for our worker.

``tasks.worker_pid`` survives a reboot; the number can then belong to an unrelated process. Every
liveness decision (extend/defer the claim) and every kill (SIGTERM/SIGKILL on timeout or reclaim)
must require the spawn-time start fingerprint to match, never bare PID existence.
"""

import os
import signal
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    conn = kbc.connect(tmp_path / "kanban.db")
    try:
        yield conn
    finally:
        conn.close()


def _claimed_running(conn, *, pid: int, started_at, max_runtime=None) -> str:
    tid = kb.create_task(conn, title="job", assignee="worker", max_runtime_seconds=max_runtime)
    kb.claim_task(conn, tid)
    kbd._set_worker_pid(conn, tid, pid)
    old = int(time.time()) - 3600
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET worker_started_at = ?, started_at = ?, claim_expires = ? WHERE id = ?",
                     (started_at, old, old, tid))
        conn.execute("UPDATE task_runs SET started_at = ? WHERE id = (SELECT current_run_id FROM tasks WHERE id = ?)",
                     (old, tid))
    return tid


def test_recycled_pid_is_reclaimed_without_being_signalled(board):
    """Our own live PID with a foreign fingerprint models a post-reboot recycle: the claim is released
    (dead worker), no signal is sent, and max-runtime enforcement does not SIGTERM the stranger either."""
    conn = board
    killed = []
    stranger_fingerprint = 1  # no live process started at tick 1
    tid = _claimed_running(conn, pid=os.getpid(), started_at=stranger_fingerprint, max_runtime=1)

    assert kbd._worker_alive(os.getpid(), stranger_fingerprint) is False
    assert tid in kbd.enforce_max_runtime(conn, signal_fn=lambda pid, sig: killed.append((pid, sig)))
    assert killed == []
    task = kb.get_task(conn, tid)
    assert task.status == "ready" and task.worker_pid is None

    tid2 = _claimed_running(conn, pid=os.getpid(), started_at=stranger_fingerprint)
    assert kb.release_stale_claims(conn, signal_fn=lambda pid, sig: killed.append((pid, sig))) == 1
    assert killed == []
    assert kb.get_task(conn, tid2).status == "ready"


def test_matching_fingerprint_keeps_the_live_worker(board):
    """The same PID with ITS OWN fingerprint (recorded at spawn) is our worker: the expired claim is
    extended rather than reclaimed, and the timeout path signals it."""
    from gateway.status import get_process_start_time

    conn = board
    killed = []
    tid = _claimed_running(conn, pid=os.getpid(), started_at=get_process_start_time(os.getpid()))
    assert kbd._worker_alive(os.getpid(), get_process_start_time(os.getpid())) is True
    assert kb.release_stale_claims(conn) == 0
    assert kb.get_task(conn, tid).status == "running"
    kinds = [e.kind for e in kb.list_events(conn, tid)]
    assert "claim_extended" in kinds

    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET max_runtime_seconds = 1 WHERE id = ?", (tid,))
    kbd.enforce_max_runtime(conn, signal_fn=lambda pid, sig: killed.append((pid, sig)))
    assert killed and killed[0] == (os.getpid(), signal.SIGTERM)


def test_same_pid_and_start_tick_on_another_boot_is_foreign(board, monkeypatch):
    """A row that survived a reboot: the PID AND the boot-relative start tick both match a process on
    this boot (the Linux start time is clock ticks since boot, so that recurs), but the persisted
    instantiation epoch does not. The worker is foreign: claim released, zero signals."""
    from gateway import drain_control

    conn = board
    killed = []
    live_fingerprint = kbd._process_fingerprint(os.getpid())
    assert live_fingerprint is not None and live_fingerprint.split("|", 1)[1] == str(
        __import__("gateway.status", fromlist=["x"]).get_process_start_time(os.getpid()))
    tid = _claimed_running(conn, pid=os.getpid(), started_at=live_fingerprint, max_runtime=1)
    assert kbd._worker_alive(os.getpid(), live_fingerprint) is True

    # Same PID, same start tick, different boot identity.
    other_boot = "deadbeef-boot:1|" + live_fingerprint.split("|", 1)[1]
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET worker_started_at = ? WHERE id = ?", (other_boot, tid))
    assert kbd._worker_alive(os.getpid(), other_boot) is False
    assert tid in kbd.enforce_max_runtime(conn, signal_fn=lambda pid, sig: killed.append((pid, sig)))
    assert killed == []
    task = kb.get_task(conn, tid)
    assert task.status == "ready" and task.worker_pid is None

    # The same value re-derived on THIS boot still identifies our worker (the witness is stable
    # within a boot, unlike the recorded epoch of a previous one).
    drain_control.current_instantiation_epoch.cache_clear()
    assert kbd._process_fingerprint(os.getpid()) == live_fingerprint


def test_unverified_fingerprint_capture_never_authorizes_a_signal(board, monkeypatch):
    """Fingerprint capture fails for a new spawn: the row is NOT a legacy NULL row. A live PID under
    it is never SIGTERM/SIGKILLed by any reclaim/timeout path, and the claim is held (not released
    beside the live process); once the PID is gone the claim is reclaimed normally."""
    from gateway import status

    conn = board
    killed = []
    monkeypatch.setattr(status, "_get_process_start_time", lambda pid: None)
    tid = kb.create_task(conn, title="job", assignee="worker", max_runtime_seconds=1)
    kb.claim_task(conn, tid)
    kbd._set_worker_pid(conn, tid, os.getpid())
    row = conn.execute("SELECT worker_started_at FROM tasks WHERE id = ?", (tid,)).fetchone()
    assert row["worker_started_at"] == kbd.UNVERIFIED_WORKER_FINGERPRINT
    monkeypatch.undo()
    old = int(time.time()) - 3600
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET started_at = ?, claim_expires = ? WHERE id = ?", (old, old, tid))
        conn.execute("UPDATE task_runs SET started_at = ? WHERE id = (SELECT current_run_id FROM tasks WHERE id = ?)",
                     (old, tid))

    sig = lambda pid, s: killed.append((pid, s))
    assert kbd.enforce_max_runtime(conn, signal_fn=sig) == []
    assert kb.release_stale_claims(conn, signal_fn=sig) == 0
    assert killed == []
    assert kb.get_task(conn, tid).status == "running"
    # An explicit operator reclaim releases the claim (human override) but still sends nothing.
    assert kb.reclaim_task(conn, tid, reason="operator", signal_fn=sig) is True
    assert killed == []

    # The process is gone (a dead PID): the row is reclaimed like any dead worker, still no signal.
    tid2 = kb.create_task(conn, title="job2", assignee="worker")
    kb.claim_task(conn, tid2)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET worker_pid = ?, worker_started_at = ?, claim_expires = ? WHERE id = ?",
                     (os.getpid(), kbd.UNVERIFIED_WORKER_FINGERPRINT, old, tid2))
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    assert kb.release_stale_claims(conn, signal_fn=sig) == 1
    assert killed == [] and kb.get_task(conn, tid2).status == "ready"


# --- t_78c2c417: macOS start-time drift reported live workers as crashed ---------------------------

def test_spawn_fingerprint_format_round_trips_through_liveness(board, monkeypatch):
    """The value written at spawn is exactly what the liveness check parses: ``<epoch>|<int>``. On
    macOS the epoch is empty, giving ``'|<digits>'``. That is valid and must read as alive."""
    import gateway.drain_control as dc
    monkeypatch.setattr(dc, "current_instantiation_epoch", lambda: "")
    fp = kbd._process_fingerprint(os.getpid())
    epoch, sep, start = fp.rpartition("|")
    assert sep == "|" and epoch == "" and start.isdigit()
    assert kbd._worker_alive(os.getpid(), fp) is True


@pytest.mark.parametrize("drift", [-150, -100, 0, 100, 150])
def test_live_worker_with_boottime_drift_is_not_crashed(board, monkeypatch, drift):
    """The 2026-10-20 evidence: spawn recorded '|179251158679', and a later read gave
    '|179251158579' (1 s drift in psutil create_time from kern.boottime). That's still our worker."""
    import gateway.status as gs
    base = 179251158679
    monkeypatch.setattr(gs, "get_process_start_time", lambda pid: base + drift)
    import gateway.drain_control as dc
    monkeypatch.setattr(dc, "current_instantiation_epoch", lambda: "")
    assert kbd._worker_alive(os.getpid(), f"|{base}") is True
    assert kbd._pid_recycled(os.getpid(), f"|{base}") is False
    assert kbd._worker_alive(os.getpid(), base) is True  # legacy integer rows too


def test_reused_pid_far_from_spawn_time_is_still_foreign(board, monkeypatch):
    import gateway.status as gs
    import gateway.drain_control as dc
    base = 179251158679
    monkeypatch.setattr(dc, "current_instantiation_epoch", lambda: "")
    monkeypatch.setattr(gs, "get_process_start_time", lambda pid: base + 60_000)  # 10 min later
    assert kbd._worker_alive(os.getpid(), f"|{base}") is False
    assert kbd._worker_alive(os.getpid(), base) is False


def test_drifted_start_on_another_epoch_is_foreign(board, monkeypatch):
    import gateway.status as gs
    import gateway.drain_control as dc
    monkeypatch.setattr(dc, "current_instantiation_epoch", lambda: "boot-B:1")
    monkeypatch.setattr(gs, "get_process_start_time", lambda pid: 1000)
    assert kbd._worker_alive(os.getpid(), "boot-A:1|1000") is False


def test_dead_pid_is_crashed_regardless_of_fingerprint(board):
    assert kbd._worker_alive(2**22 + 12345, "|179251158679") is False


def test_live_drifted_worker_is_not_released_by_dispatcher(board, monkeypatch):
    import gateway.status as gs
    import gateway.drain_control as dc
    base = 179251158679
    monkeypatch.setattr(dc, "current_instantiation_epoch", lambda: "")
    monkeypatch.setattr(gs, "get_process_start_time", lambda pid: base - 100)
    conn = board
    tid = _claimed_running(conn, pid=os.getpid(), started_at=f"|{base}")
    killed = []
    kb.release_stale_claims(conn, signal_fn=lambda pid, sig: killed.append((pid, sig)))
    assert killed == []
    task = kb.get_task(conn, tid)
    assert task.status == "running" and task.worker_pid == os.getpid()
