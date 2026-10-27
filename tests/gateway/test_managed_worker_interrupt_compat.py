"""Regression (t_44279928): a ManagedWorker reached through request_hard_interrupt during gateway
shutdown must accept the positional message, or stop_authority_work aborts with TypeError and the
profile runtime stays ``runtime_draining`` (cron/kanban owners refused) until the next restart."""
from gateway.session_managed_worker import ManagedWorker
from agent.interrupt_compat import request_hard_interrupt


def _worker():
    worker = ManagedWorker.__new__(ManagedWorker)  # no subprocess
    worker.sent = []
    worker.control = worker.sent.append
    return worker


def test_request_hard_interrupt_with_message_stops_managed_worker():
    worker = _worker()
    assert request_hard_interrupt(worker, 'Profile stopping', tool_reason='gateway shutdown') is True
    assert worker.sent == [{'type': 'stop'}]


def test_managed_worker_interrupt_without_message_still_works():
    worker = _worker()
    worker.interrupt()
    assert worker.sent == [{'type': 'stop'}]
