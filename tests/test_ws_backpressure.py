"""Slow peers must not block healthy peers or corrupt concurrent state frames."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import WebSocketDisconnect

from inverter_dashboard import websocket_handler as wsh


class OrderedPeers(set):
    def __iter__(self):
        return iter(sorted(super().__iter__(), key=lambda peer: peer.name))


class Peer:
    def __init__(self, name, *, blocked=False, failed=False, blocked_close=False):
        self.name = name
        self.blocked = blocked
        self.failed = failed
        self.blocked_close = blocked_close
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.received = asyncio.Event()
        self.receive_release = asyncio.Event()
        self.messages = []
        self.close_codes = []
        self.active_writes = 0
        self.maximum_writes = 0
        self.cancelled_writes = 0
        self.receive_calls = 0

    async def accept(self):
        return None

    async def send_text(self, message):
        self.active_writes += 1
        self.maximum_writes = max(self.maximum_writes, self.active_writes)
        self.entered.set()
        try:
            if self.blocked:
                await self.release.wait()
            if self.failed:
                raise OSError("peer transport failed")
            self.messages.append(json.loads(message))
            self.received.set()
        except asyncio.CancelledError:
            self.cancelled_writes += 1
            raise
        finally:
            self.active_writes -= 1

    async def close(self, code):
        self.close_codes.append(code)
        if self.blocked_close:
            await asyncio.Event().wait()

    async def receive_json(self):
        self.receive_calls += 1
        await self.receive_release.wait()
        raise WebSocketDisconnect()


@pytest.fixture(autouse=True)
def isolate_clients(monkeypatch):
    monkeypatch.setattr(wsh, "ws_clients", OrderedPeers())
    monkeypatch.setattr(wsh, "_ws_write_locks", {})
    monkeypatch.setattr(wsh, "_broadcast_lock", asyncio.Lock())
    monkeypatch.setattr(wsh, "WS_SEND_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(wsh, "WS_CLOSE_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(wsh, "build_payload", lambda: {"sequence": 1})


async def test_healthy_peer_receives_while_first_peer_is_blocked(monkeypatch):
    monkeypatch.setattr(wsh, "WS_SEND_TIMEOUT_SECONDS", 2)
    slow = Peer("a-slow", blocked=True)
    healthy = Peer("z-healthy")
    wsh.ws_clients.update([slow, healthy])
    task = asyncio.create_task(wsh.broadcast_state())
    try:
        await asyncio.wait_for(slow.entered.wait(), 1)
        await asyncio.wait_for(healthy.received.wait(), 1)
        assert not slow.messages
        assert not task.done()
    finally:
        slow.release.set()
        await task
    assert slow.messages == healthy.messages == [{"sequence": 1}]


async def test_stalled_send_and_close_are_bounded_and_peer_is_removed():
    slow = Peer("slow", blocked=True, blocked_close=True)
    wsh.ws_clients.add(slow)
    await asyncio.wait_for(wsh.broadcast_state(), 1)
    assert slow.cancelled_writes == 1
    assert slow.close_codes == [1013]
    assert slow not in wsh.ws_clients
    assert slow not in wsh._ws_write_locks
    assert slow.active_writes == 0


async def test_failed_peer_does_not_cancel_healthy_sends():
    broken = Peer("a-broken", failed=True)
    healthy = Peer("z-healthy")
    wsh.ws_clients.update([broken, healthy])
    await wsh.broadcast_state()
    assert healthy.messages == [{"sequence": 1}]
    assert healthy in wsh.ws_clients
    assert broken not in wsh.ws_clients
    assert broken.close_codes == [1013]


async def test_overlapping_broadcasts_keep_every_frame_in_order(monkeypatch):
    values = iter([1, 2, 3])
    monkeypatch.setattr(wsh, "build_payload", lambda: {"sequence": next(values)})
    peer = Peer("peer", blocked=True)
    wsh.ws_clients.add(peer)
    first = asyncio.create_task(wsh.broadcast_state())
    await peer.entered.wait()
    others = [asyncio.create_task(wsh.broadcast_state()) for _ in range(2)]
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert peer.maximum_writes == 1
    peer.release.set()
    await asyncio.gather(first, *others)
    assert peer.maximum_writes == 1
    assert peer.messages == [{"sequence": i} for i in (1, 2, 3)]


async def test_queued_writes_do_not_retry_a_timed_out_peer():
    peer = Peer("peer", blocked=True)
    wsh.ws_clients.add(peer)
    first = asyncio.create_task(wsh.broadcast_state())
    await peer.entered.wait()
    second = asyncio.create_task(wsh.broadcast_state())
    await asyncio.wait_for(asyncio.gather(first, second), 1)
    assert peer.cancelled_writes == 1
    assert peer.maximum_writes == 1
    assert peer.close_codes == [1013]
    assert not peer.messages


async def test_cancelled_broadcast_leaves_no_running_send_tasks():
    peers = [Peer(str(i), blocked=True) for i in range(3)]
    wsh.ws_clients.update(peers)
    task = asyncio.create_task(wsh.broadcast_state())
    await asyncio.gather(*(peer.entered.wait() for peer in peers))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert all(peer.active_writes == 0 and peer.cancelled_writes == 1 for peer in peers)
    for peer in peers:
        peer.release.set()
    await wsh.broadcast_state()
    assert all(peer.messages == [{"sequence": 1}] for peer in peers)


async def test_initial_state_and_broadcast_share_one_ordered_writer(monkeypatch):
    values = iter([1, 2])
    monkeypatch.setattr(wsh, "build_payload", lambda: {"sequence": next(values)})
    peer = Peer("peer", blocked=True)
    handler = asyncio.create_task(wsh.handle_websocket(peer, SimpleNamespace(mqtt_client=None)))
    await peer.entered.wait()
    broadcast = asyncio.create_task(wsh.broadcast_state())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert peer.maximum_writes == 1
    peer.release.set()
    await broadcast
    peer.receive_release.set()
    await handler
    assert peer.messages == [{"sequence": 1}, {"sequence": 2}]
    assert peer not in wsh.ws_clients
    assert peer not in wsh._ws_write_locks


async def test_initial_send_timeout_never_enters_command_receive_loop():
    peer = Peer("peer", blocked=True)
    await asyncio.wait_for(wsh.handle_websocket(peer, SimpleNamespace(mqtt_client=None)), 1)
    assert peer.receive_calls == 0
    assert peer.cancelled_writes == 1
    assert peer not in wsh.ws_clients
    assert peer not in wsh._ws_write_locks


async def test_no_clients_does_not_build_payload(monkeypatch):
    def unexpected():
        pytest.fail("No subscribers need no payload")

    monkeypatch.setattr(wsh, "build_payload", unexpected)
    await wsh.broadcast_state()


async def test_waiting_broadcasts_do_not_allocate_more_payloads(monkeypatch):
    built = []

    def payload():
        built.append(len(built) + 1)
        return {"sequence": built[-1]}

    monkeypatch.setattr(wsh, "build_payload", payload)
    peer = Peer("peer", blocked=True)
    wsh.ws_clients.add(peer)
    first = asyncio.create_task(wsh.broadcast_state())
    await peer.entered.wait()
    waiting = [asyncio.create_task(wsh.broadcast_state()) for _ in range(20)]
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert built == [1]
    assert peer.maximum_writes == 1
    peer.release.set()
    await asyncio.gather(first, *waiting)
    assert built == list(range(1, 22))
    assert peer.messages == [{"sequence": i} for i in built]


async def test_new_connection_can_receive_initial_frame_during_fanout(monkeypatch):
    monkeypatch.setattr(wsh, "WS_SEND_TIMEOUT_SECONDS", 2)
    slow = Peer("slow", blocked=True)
    wsh.ws_clients.add(slow)
    broadcast = asyncio.create_task(wsh.broadcast_state())
    await slow.entered.wait()
    new = Peer("new")
    handler = asyncio.create_task(wsh.handle_websocket(new, SimpleNamespace(mqtt_client=None)))
    try:
        await asyncio.wait_for(new.received.wait(), 1)
        assert not broadcast.done()
        assert new.messages == [{"sequence": 1}]
    finally:
        slow.release.set()
        new.receive_release.set()
        await asyncio.gather(broadcast, handler)
    assert new not in wsh._ws_write_locks


async def test_cancelling_waiting_broadcast_does_not_stall_following_call(monkeypatch):
    values = iter([1, 2])
    monkeypatch.setattr(wsh, "build_payload", lambda: {"sequence": next(values)})
    peer = Peer("peer", blocked=True)
    wsh.ws_clients.add(peer)
    first = asyncio.create_task(wsh.broadcast_state())
    await peer.entered.wait()
    waiting = asyncio.create_task(wsh.broadcast_state())
    await asyncio.sleep(0)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    peer.release.set()
    await first
    await wsh.broadcast_state()
    assert peer.messages == [{"sequence": 1}, {"sequence": 2}]
