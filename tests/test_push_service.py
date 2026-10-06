"""Durable delivery, source cancellation and capability revocation at dispatch."""

import asyncio
import logging
import time
from unittest.mock import AsyncMock

import pytest
from py_vapid import VapidException

from inverter_dashboard.push_events import DEFAULT_PREFERENCES, event_payload
from inverter_dashboard.push_service import PushService, _delivery_log_context, _log_provider_status
from tests.test_push_transport import receiver


@pytest.mark.parametrize(
    "error,category",
    [
        (TimeoutError("private endpoint capability"), "timeout"),
        (ImportError("private endpoint capability"), "dependency"),
        (RuntimeError("private endpoint capability"), "unexpected"),
        (VapidException("private endpoint capability"), "vapid"),
        (ValueError("private endpoint capability"), "validation"),
    ],
)
async def test_delivery_failure_diagnostics_are_bounded_and_secret_free(
    service, caplog, error, category
):
    current, subscription = service
    delivery = queue(current)
    current.transport.send = AsyncMock(side_effect=error)
    with caplog.at_level(logging.INFO, logger="inverter_dashboard.push_service"):
        await current._deliver(delivery)
    assert f"category={category} kind=native attempt=1" in caplog.text
    assert "private endpoint capability" not in caplog.text
    assert subscription["endpoint"] not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


async def test_provider_acceptance_logs_status_not_browser_delivery(service, caplog):
    current, subscription = service
    delivery = queue(current)
    current.transport.send = AsyncMock(return_value=201)
    with caplog.at_level(logging.INFO, logger="inverter_dashboard.push_service"):
        await current._deliver(delivery)
    assert "Web Push provider response status=201 kind=native attempt=1" in caplog.text
    assert subscription["endpoint"] not in caplog.text


@pytest.mark.parametrize(
    "bad_attempt,bad_status",
    [(99, "private endpoint"), (True, True), (-1, 99), ("private endpoint", 600)],
)
def test_diagnostic_values_cannot_expand_into_private_payload_text(caplog, bad_attempt, bad_status):
    kind, attempt = _delivery_log_context(
        {"payload": {"kind": "private endpoint"}, "attempts": bad_attempt}
    )
    assert (kind, attempt) == ("unknown", "unknown")
    with caplog.at_level(logging.INFO, logger="inverter_dashboard.push_service"):
        _log_provider_status(bad_status, kind, attempt)
    assert "category=invalid_status kind=unknown attempt=unknown" in caplog.text
    assert "private endpoint" not in caplog.text


@pytest.fixture
async def service(tmp_path):
    result = PushService(tmp_path, "https://github.com/victron-venus/inverter-dashboard")
    subscription = receiver()[2]
    result.register(subscription, dict(DEFAULT_PREFERENCES))
    result.connect("igw", object())
    yield result, subscription
    await result.close()


def queue(service, suffix="1"):
    now = time.time()
    service.queue(
        event_payload(
            "native", "victron", suffix, int(now * 1000), now, title="Alarm", body="Native event"
        )
    )
    return service.store.next_delivery(now + 0.1, claim=True)


async def test_success_and_dedupe_across_process_restart(service, tmp_path):
    current, _ = service
    delivery = queue(current)
    current.transport.send = AsyncMock(return_value=201)
    await current._deliver(delivery)
    assert current.store.next_delivery(time.time()) is None
    original_key = current.transport.public_key
    current.store.close()
    # A new process owner keeps event and VAPID identity after orderly shutdown.
    restarted = PushService(tmp_path, "https://github.com/victron-venus/inverter-dashboard")
    try:
        assert restarted.transport.public_key == original_key
        assert restarted.store.count() == 1
        assert not restarted.store.remember(delivery["event_id"], time.time())
    finally:
        await restarted.close()


@pytest.mark.parametrize("status", [404, 410])
async def test_terminal_provider_response_removes_subscription_and_all_pending(service, status):
    current, subscription = service
    delivery = queue(current)
    queue(current, "another")
    current.transport.send = AsyncMock(return_value=status)
    await current._deliver(delivery)
    assert current.registered(subscription["endpoint"]) is None
    assert current.store.next_delivery(time.time() + 1) is None


@pytest.mark.parametrize(
    "status,retry", [(201, False), (302, False), (403, False), (429, True), (503, True)]
)
async def test_only_transient_statuses_retry_within_bounded_attempts(
    service, status, retry, monkeypatch
):
    current, _ = service
    clock = [time.time()]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    delivery = queue(current)
    current.transport.send = AsyncMock(return_value=status)
    await current._deliver(delivery)
    clock[0] += 21
    next_item = current.store.next_delivery(clock[0])
    assert (next_item is not None) == retry
    if retry:
        assert next_item["attempts"] == 1
        await current._deliver(next_item)
        clock[0] += 21
        final = current.store.next_delivery(clock[0])
        assert final["attempts"] == 2
        await current._deliver(final)
        assert current.store.next_delivery(clock[0] + 100) is None


async def test_retired_epoch_checked_before_send_and_inflight_cancelled(service):
    current, _ = service
    delivery = queue(current)
    current.disconnect()
    current.transport.send = AsyncMock(return_value=201)
    await current._deliver(delivery)
    current.transport.send.assert_not_called()
    current.connect("mqtt", object())
    delivery = queue(current, "new")
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def blocked_send(*_args):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    current.transport.send = blocked_send
    sending = asyncio.create_task(current._deliver(delivery))
    await started.wait()
    current.disconnect()
    await asyncio.wait_for(asyncio.gather(sending), 1)
    assert cancelled.is_set()
    assert current.store.next_delivery(time.time() + 1) is None


@pytest.mark.parametrize("change", ["delete", "disable"])
async def test_subscription_revocation_cancels_inflight_and_pending(service, change):
    current, subscription = service
    delivery = queue(current)
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def send(*_args):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    current.transport.send = send
    sending = asyncio.create_task(current._deliver(delivery))
    await entered.wait()
    if change == "delete":
        current.delete(subscription["endpoint"])
    else:
        current.register(subscription, {key: False for key in DEFAULT_PREFERENCES})
    await asyncio.wait_for(asyncio.gather(sending), 1)
    assert cancelled.is_set()
    assert current.store.next_delivery(time.time()) is None


async def test_two_workers_claim_each_delivery_once_and_shutdown_cleanly(service):
    current, _ = service
    queue(current)
    # Undo the test helper's claim so actual workers can find the item immediately.
    current.store.db.execute("UPDATE deliveries SET next_at=0")
    current.store.db.commit()
    delivered = asyncio.Event()

    async def send(*_args):
        delivered.set()
        return 201

    current.transport.send = AsyncMock(side_effect=send)
    current.start()
    await asyncio.wait_for(delivered.wait(), 1)
    await asyncio.sleep(0)
    assert current.transport.send.await_count == 1


async def test_full_delivery_queue_does_not_report_test_as_queued(service, monkeypatch):
    from inverter_dashboard import push_store
    from inverter_dashboard.push_service import PushRateLimit

    current, subscription = service
    monkeypatch.setattr(push_store, "MAX_DELIVERIES", 1)
    queue(current)
    with pytest.raises(PushRateLimit):
        current.test(subscription["endpoint"])


async def test_optional_storage_failure_disables_push_without_breaking_source_processing(
    service, monkeypatch
):
    import sqlite3

    from inverter_dashboard.push_observer import PushObserver
    from inverter_dashboard.server import MqttState

    current, _ = service

    def broken(*_args):
        raise sqlite3.DatabaseError("storage failed")

    monkeypatch.setattr(current.store, "retire_epochs", broken)
    PushObserver(current).snapshot(MqttState())
    assert not current.available and current.connection is None
    assert current.status()["reason"] == "storage_unavailable"
    current.disconnect()  # Idempotent even with an unavailable DB.


@pytest.mark.parametrize(
    "damage",
    [
        "subscription_json",
        "preferences_json",
        "delivery_json",
        "delivery_shape",
        "vapid_missing",
        "vapid_corrupt",
    ],
)
async def test_corrupt_persisted_rows_fail_closed_without_reset_or_key_regeneration(
    tmp_path, damage
):
    import sqlite3

    service = PushService(tmp_path, "https://github.com/victron-venus/inverter-dashboard")
    subscription = receiver()[2]
    service.register(subscription, dict(DEFAULT_PREFERENCES))
    service.test(subscription["endpoint"])
    await service.close()
    path = tmp_path / "push.sqlite3"
    db = sqlite3.connect(path)
    statements = {
        "subscription_json": "UPDATE subscriptions SET subscription='{'",
        "preferences_json": "UPDATE subscriptions SET preferences='{'",
        "delivery_json": "UPDATE deliveries SET payload='{'",
        "delivery_shape": "UPDATE deliveries SET payload='{}'",
        "vapid_missing": "DELETE FROM metadata WHERE key='vapid_private_pem'",
        "vapid_corrupt": "UPDATE metadata SET value=x'0001' WHERE key='vapid_private_pem'",
    }
    db.execute(statements[damage])
    db.commit()
    db.close()
    before = path.read_bytes()
    broken = PushService(tmp_path, "https://github.com/victron-venus/inverter-dashboard")
    assert not broken.available and broken.status()["reason"] == "storage_unavailable"
    broken.start()
    assert not broken.workers
    await broken.close()
    assert path.read_bytes() == before


@pytest.mark.parametrize("change", ["reregister", "preferences"])
async def test_completed_old_provider_result_cannot_delete_updated_registration(service, change):
    current, subscription = service
    delivery = queue(current)
    updated = {**DEFAULT_PREFERENCES, "water": False}

    def change_registration():
        if change == "reregister":
            current.delete(subscription["endpoint"])
        current.register(subscription, updated)

    async def completed_before_continuation(*_args):
        # Queue update before the awaiting delivery worker resumes. The send is
        # already done when revocation calls cancel(), which cannot undo it.
        asyncio.get_running_loop().call_soon(change_registration)
        return 410

    current.transport.send = completed_before_continuation
    await current._deliver(delivery)
    registered = current.registered(subscription["endpoint"])
    assert registered is not None and registered["preferences"] == updated
    assert current.store.next_delivery(time.time()) is None


async def test_missing_database_after_initialized_store_does_not_rotate_vapid(tmp_path):
    service = PushService(tmp_path, "https://github.com/victron-venus/inverter-dashboard")
    assert service.available
    await service.close()
    marker = (tmp_path / "push.lock").read_bytes()
    assert marker == b"initialized-v1\n"
    (tmp_path / "push.sqlite3").unlink()
    unavailable = PushService(tmp_path, "https://github.com/victron-venus/inverter-dashboard")
    assert not unavailable.available
    assert not (tmp_path / "push.sqlite3").exists()
    assert (tmp_path / "push.lock").read_bytes() == marker
    await unavailable.close()
