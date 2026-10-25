"""A Desktop (LOCAL route) /heartbeat fires through the gateway poller (t_c1764656).

The poller's watch holds a source rebuilt from the routing index (or the slash event's copy), never
the live session's own source object; ``snapshot_local_automation`` checks identity, so the tick was
refused with ``permission_denied``, refunded, and logged at DEBUG only: armed forever, fire_count 0.
"""
import dataclasses
import json

import pytest

from gateway.config import GatewayConfig
from gateway.run import GatewayRunner
from gateway.session_authority import SessionAuthority, initialize_session_authority
from gateway.session_contract import Principal
from hermes_cli.heartbeat import HeartbeatState, load_heartbeat, save_heartbeat
from hermes_state_runtime import list_session_admissions


async def _local_heartbeat(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(SessionAuthority, '_schedule', lambda self, ref: None)
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / 'sessions'))
    from hermes_state import SessionDB
    db = SessionDB(tmp_path / 'state.db')
    runner.session_store._db = db
    runner._session_db = db
    authority = await initialize_session_authority(runner, profile_id='default', instance_id='hb-local', db=db)
    from gateway.session_local import create_local_session
    actor = Principal('test-owner', 'default', frozenset({'session:create', 'session:submit', 'session:read'}), 'fixture')
    ref = create_local_session(authority, actor, dict(request_id='desk', source='gui', model='fixture', toolsets=[]))
    live = authority.sessions[ref.session_id]
    entry = runner.session_store.lookup_by_session_key(live.route)
    save_heartbeat(entry.session_id, HeartbeatState(prompt='status watch', interval_seconds=60, created_at=1))
    return runner, authority, ref, live, entry


@pytest.mark.asyncio
@pytest.mark.parametrize('copy_source', [False, True], ids=['live-source', 'restored-copy'])
async def test_local_route_heartbeat_is_admitted_by_the_gateway_poller(tmp_path, monkeypatch, copy_source):
    runner, authority, ref, live, entry = await _local_heartbeat(tmp_path, monkeypatch)
    try:
        # The routing-index restore / slash path hands the poller an equal but distinct object.
        source = dataclasses.replace(live.source) if copy_source else live.source
        assert source is not live.source or not copy_source
        watch = {live.route: (source, entry.session_id)}
        await runner._heartbeat_poll_watch(watch, live.route, source, entry.session_id)
        rows = list_session_admissions(authority.db, session_id=ref.session_id)
        beats = [r for r in rows if r['payload'].get('local_automation_v1', {}).get('heartbeat')]
        assert len(beats) == 1, rows
        assert 'status watch' in beats[0]['payload']['text']
        assert load_heartbeat(entry.session_id).fire_count == 1
        assert json.loads(beats[0]['request_id'])[0] == 'heartbeat'
        # The next poll inside the interval coalesces behind the pending admission: no second row.
        await runner._heartbeat_poll_watch(watch, live.route, source, entry.session_id)
        assert len(list_session_admissions(authority.db, session_id=ref.session_id)) == 1
    finally:
        authority.db.close()
