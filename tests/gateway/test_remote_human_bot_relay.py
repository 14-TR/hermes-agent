"""Remote-human Bot Chat deliveries (Teams bridge) on the v0.22.5 authority path (t_6b17b252).

The marker rides ``bot_relay.deliver`` -> admitted automation descriptor -> restored event ->
the executing turn's ``remote_human_channel`` contextvar, so approval/clarify stay text-only.
"""
from types import SimpleNamespace

import pytest

from gateway.config import GatewayConfig
from gateway.run import GatewayRunner
from gateway.session_authority import SessionAuthority, initialize_session_authority
from gateway.session_contract import Principal
from hermes_state_runtime import RuntimeStoreError, get_session_admission


async def _authority(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(SessionAuthority, '_schedule', lambda self, ref: None)
    monkeypatch.setattr('gateway.session_bot._watch_reply', lambda *args: None)
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / 'sessions'))
    from hermes_state import SessionDB
    db = SessionDB(tmp_path / 'state.db')
    runner.session_store._db = db
    runner._session_db = db
    authority = await initialize_session_authority(runner, profile_id='default', instance_id='remote-fixture', db=db)
    return runner, authority


def test_client_forwards_remote_channel_only_when_set(tmp_path, monkeypatch):
    from tools import bot_live_delivery as live
    calls = []
    monkeypatch.setattr(live, 'authority_delivery', lambda home, params: calls.append(params) or {})
    owner = dict(profile_home=str(tmp_path), session_id='bot', canonical=True, lease_id='f', live_session_id='bot')
    live.deliver_to_live_owner(tmp_path, owner, 'hi', remote_channel='teams')
    live.deliver_to_live_owner(tmp_path, owner, 'hi')
    assert calls[0]['remote_channel'] == 'teams'
    assert 'remote_channel' not in calls[1]  # old fingerprint unchanged for desktop senders


@pytest.mark.asyncio
async def test_remote_channel_is_committed_restored_and_part_of_identity(tmp_path, monkeypatch):
    from gateway.session_automation import restore_local_automation
    from gateway.session_bot import deliver
    from gateway.session_local import create_local_session
    from gateway.session_local_title import title_new_session
    _runner, authority = await _authority(tmp_path, monkeypatch)
    actor = Principal('test-owner', 'default', frozenset({'session:create', 'session:submit', 'session:read'}), 'fixture')
    try:
        ref = create_local_session(authority, actor, dict(request_id='bot', source='gui', model='fixture', toolsets=[]))
        title_new_session(authority, ref, 'Bot Chat')
        connection = SimpleNamespace(authority=authority, actor=actor)
        params = dict(id='a' * 32, profile='default', message='from phone', remote_channel='teams')
        first = await deliver(connection, params)
        assert (await deliver(connection, params))['admission_id'] == first['admission_id']
        row = get_session_admission(authority.db, admission_id=first['admission_id'])
        assert row['payload']['local_automation_v1']['remote_human_channel'] == 'teams'
        assert restore_local_automation(authority, ref, row).metadata['remote_human_channel'] == 'teams'
        # Same id without the marker is a different delivery, never a silent downgrade.
        with pytest.raises(RuntimeStoreError, match='admission_conflict'):
            await deliver(connection, {k: v for k, v in params.items() if k != 'remote_channel'})
        with pytest.raises(RuntimeStoreError, match='invalid_params'):
            await deliver(connection, dict(params, id='b' * 32, remote_channel='Teams; rm'))
        # A desktop delivery carries no marker.
        plain = await deliver(connection, dict(id='c' * 32, profile='default', message='desktop'))
        row = get_session_admission(authority.db, admission_id=plain['admission_id'])
        assert 'remote_human_channel' not in row['payload']['local_automation_v1']
        assert 'remote_human_channel' not in restore_local_automation(authority, ref, row).metadata
    finally:
        authority.db.close()


def test_ingress_binds_marker_for_the_turn():
    """The executing drain binds the contextvar the approval/clarify gates read."""
    import inspect
    from gateway import session_ingress
    src = inspect.getsource(session_ingress)
    assert "set_remote_human_context(str(event.metadata.get('remote_human_channel')" in src
    assert 'reset_remote_human_context(remote_token)' in src
