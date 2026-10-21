"""An open canonical Bot Chat is the profile's live-delivery consumer (t_208f02b4).

Ordinary chats claim their active-session lease lazily on the first turn. A Bot Chat that
is merely open never did, so ``find_canonical_live_owner`` returned None and Teams webhook
deliveries parked as "awaiting owner" while another desktop chat held the only lease.

Contract:
- resuming the canonical Bot Chat claims its consumer lease immediately;
- a non-Bot-Chat resume stays lazy (no slot held);
- the registry holds several consumers per profile; another session's lease never
  hides or evicts the Bot Chat owner.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import tui_gateway.server as srv


def _ok(sid):
    return {"jsonrpc": "2.0", "id": 1, "result": {"session_id": sid}}


@pytest.fixture
def claims(monkeypatch):
    seen = []

    def fake_ensure(sid, session):
        seen.append(sid)
        session["active_session_lease"] = object()

    monkeypatch.setattr(srv, "_ensure_active_session_slot", fake_ensure)
    return seen


def test_resume_of_bot_chat_claims_consumer_lease(monkeypatch, claims):
    session = {"active_session_lease": None}
    monkeypatch.setitem(srv._sessions, "s-bot", session)
    ctx = SimpleNamespace(found={"title": "Bot Chat", "model_config": None})
    srv._claim_bot_chat_consumer(ctx, _ok("s-bot"))
    assert claims == ["s-bot"]
    assert session["active_session_lease"] is not None


def test_resume_of_ordinary_chat_stays_lazy(monkeypatch, claims):
    session = {"active_session_lease": None}
    monkeypatch.setitem(srv._sessions, "s-other", session)
    ctx = SimpleNamespace(found={"title": "Daily notes", "model_config": None})
    srv._claim_bot_chat_consumer(ctx, _ok("s-other"))
    assert claims == []
    assert session["active_session_lease"] is None


def test_claim_failure_never_breaks_resume(monkeypatch):
    def boom(sid, session):
        raise RuntimeError("registry down")

    monkeypatch.setattr(srv, "_ensure_active_session_slot", boom)
    monkeypatch.setitem(srv._sessions, "s-bot2", {"active_session_lease": None})
    resp = _ok("s-bot2")
    ctx = SimpleNamespace(found={"title": "Bot Chat"})
    assert srv._claim_bot_chat_consumer(ctx, resp) is resp


def test_two_consumers_coexist_and_bot_chat_is_owner(tmp_path, monkeypatch):
    from hermes_cli.active_sessions import try_acquire_active_session
    from tools import bot_live_delivery as bld

    home = tmp_path / "big"
    (home / "runtime").mkdir(parents=True)
    (home / "state.db").write_bytes(b"")

    class FakeDB:
        def __init__(self, *a, **k):
            pass

        def get_session_by_title(self, title):
            return {"id": "botchat"} if title == "Bot Chat" else None

        def get_compression_tip(self, sid):
            return sid

        def close(self):
            pass

    monkeypatch.setattr("hermes_state.SessionDB", FakeDB)
    meta = lambda live: {"live_session_id": live, "bot_live_delivery_consumer": True}
    other, err = try_acquire_active_session(session_id="other", surface="cli", config={},
                                            metadata=meta("o1"), registry_home=home)
    assert err is None
    assert bld.find_canonical_live_owner(home) is None  # the original bug shape
    bot, err = try_acquire_active_session(session_id="botchat", surface="cli", config={},
                                          metadata=meta("b1"), registry_home=home)
    assert err is None
    owner = bld.find_canonical_live_owner(home)
    assert owner and owner["session_id"] == "botchat" and owner["live_session_id"] == "b1"
    # A third consumer (e.g. a cron/DM CLI turn) neither hides nor evicts the Bot Chat lease.
    third, err = try_acquire_active_session(session_id="cron-1", surface="cli", config={},
                                            metadata=meta("c1"), registry_home=home)
    assert err is None
    assert bld.find_canonical_live_owner(home)["lease_id"] == bot.lease_id
    # And nobody else can take the Bot Chat session itself while it is held.
    _, err = try_acquire_active_session(session_id="botchat", surface="cli", config={},
                                        metadata=meta("x9"), registry_home=home)
    assert err is not None
    for lease in (other, bot, third):
        lease.release()
