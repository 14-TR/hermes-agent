"""t_5f5641bd: the canonical ``bot_relay.deliver`` contract must accept every key the client
sends. ``tools.bot_live_delivery._door`` adds ``remote_channel`` for Teams deliveries
(t_1b19a76f); when the closed contract lacked it, the owner refused every Teams message with
``invalid_params`` before the handler ran."""
from pathlib import Path

from tui_gateway.contracts.registry import CANONICAL_METHODS, canonical_param_problems


def _door_params(tmp_path, monkeypatch, **kwargs):
    import tools.bot_live_delivery as m

    captured = {}
    monkeypatch.setattr(m, "_owner", lambda home, owner: {"session_id": "s1"})
    home = tmp_path / "profiles" / "big"
    home.mkdir(parents=True)
    m._door(lambda h, params: captured.setdefault("params", params) and {}, home, {}, "hello",
            delivery_id="a" * 64, **kwargs)
    return captured["params"]


def test_remote_channel_delivery_passes_contract(tmp_path, monkeypatch):
    params = _door_params(tmp_path, monkeypatch, remote_channel="teams")
    assert params["remote_channel"] == "teams"
    assert canonical_param_problems(CANONICAL_METHODS["bot_relay.deliver"], params) == []


def test_plain_delivery_still_passes_contract(tmp_path, monkeypatch):
    params = _door_params(tmp_path, monkeypatch)
    assert "remote_channel" not in params
    assert canonical_param_problems(CANONICAL_METHODS["bot_relay.deliver"], params) == []


def test_unknown_key_still_refused():
    problems = canonical_param_problems(CANONICAL_METHODS["bot_relay.deliver"],
                                        {"id": "a" * 64, "message": "x", "bogus": 1})
    assert problems
