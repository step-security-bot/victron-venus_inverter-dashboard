"""Persistence and bounded delivery behavior, using private temporary stores."""

import os
import sqlite3

import pytest

from inverter_dashboard import push_store
from inverter_dashboard.push_store import PushStore


def subscription(number=1):
    return {"endpoint": f"https://fcm.googleapis.com/fcm/send/{number}", "keys": {}}


def test_restart_preserves_key_subscription_and_dedupe(tmp_path):
    path = tmp_path / "private"
    first = PushStore(path)
    assert first.initialize_metadata("vapid", b"private-test-key") == b"private-test-key"
    sid = first.register(subscription(), {"native": True}, 100)
    assert first.enqueue("event-1", {"title": "Warning"}, [sid], 100)
    first.close()
    second = PushStore(path)
    assert second.initialize_metadata("vapid", b"must-not-replace") == b"private-test-key"
    assert second.get(sid)["preferences"] == {"native": True}
    assert not second.enqueue("event-1", {"title": "Warning"}, [sid], 101)
    assert second.next_delivery(101)["event_id"] == "event-1"
    assert os.stat(path).st_mode & 0o777 == 0o700
    assert os.stat(path / "push.sqlite3").st_mode & 0o777 == 0o600
    second.close()


def test_event_reservation_rolls_back_with_failed_delivery(tmp_path):
    store = PushStore(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        store.enqueue("event", {"title": "Warning"}, ["nonexistent"], 100)
    assert store.remember("event", 101)
    store.close()


def test_unsubscribe_removes_queued_deliveries(tmp_path):
    store = PushStore(tmp_path)
    sid = store.register(subscription(), {}, 100)
    store.enqueue("event", {}, [sid], 100)
    store.delete(sid)
    assert store.next_delivery(101) is None
    assert store.get(sid) is None
    store.close()


def test_limits_update_existing_but_reject_new_subscriptions(tmp_path, monkeypatch):
    monkeypatch.setattr(push_store, "MAX_SUBSCRIPTIONS", 1)
    monkeypatch.setattr(push_store, "MAX_EVENTS", 2)
    monkeypatch.setattr(push_store, "MAX_DELIVERIES", 1)
    store = PushStore(tmp_path)
    sid = store.register(subscription(), {}, 100)
    assert store.register(subscription(), {"native": False}, 101) == sid
    with pytest.raises(ValueError, match="limit"):
        store.register(subscription(2), {}, 102)
    for i in range(3):
        store.enqueue(f"e{i}", {}, [sid], 110 + i)
    assert store.db.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0] == 1
    assert store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 2
    assert not store.remember("e2", 113)
    store.close()


def test_retry_and_expiration_are_bounded(tmp_path):
    store = PushStore(tmp_path)
    sid = store.register(subscription(), {}, 100)
    assert store.allow_test(sid, 100)
    assert not store.allow_test(sid, 101)
    assert store.allow_test(sid, 160)
    store.enqueue("event", {}, [sid], 100)
    for now in (100, 120, 140):
        item = store.next_delivery(now)
        assert item is not None
        store.finish(item, retry_at=now + 20)
        assert store.next_delivery(now + 1) is None
    assert store.next_delivery(160) is None
    store.enqueue("expired", {}, [sid], 200)
    assert store.next_delivery(501) is None
    store.close()


def test_symlinks_are_not_followed(tmp_path):
    actual = tmp_path / "target"
    actual.mkdir()
    linked = tmp_path / "link"
    linked.symlink_to(actual)
    with pytest.raises(ValueError):
        PushStore(linked)
    (actual / "push.sqlite3").symlink_to(tmp_path / "outside")
    with pytest.raises(ValueError):
        PushStore(actual)
    assert not (tmp_path / "outside").exists()


def test_second_process_cannot_open_or_modify_locked_store(tmp_path):
    import subprocess
    import sys

    store = PushStore(tmp_path)
    store.initialize_metadata("test", b"preserved")
    before = (tmp_path / "push.sqlite3").read_bytes()
    code = "from pathlib import Path; from inverter_dashboard.push_store import PushStore; import sys; PushStore(Path(sys.argv[1]))"
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)], capture_output=True, check=False
    )
    assert result.returncode != 0
    assert (tmp_path / "push.sqlite3").read_bytes() == before
    assert store.metadata("test") == b"preserved"
    store.close()
    reopened = PushStore(tmp_path)
    assert reopened.metadata("test") == b"preserved"
    reopened.close()


@pytest.mark.parametrize(
    "name",
    ["push.sqlite3", "push.lock", "push.sqlite3-wal", "push.sqlite3-shm", "push.sqlite3-journal"],
)
def test_hardlinked_store_files_rejected_before_chmod_and_preserve_target(tmp_path, name):
    outside = tmp_path / "outside"
    outside.write_bytes(b"must remain untouched")
    outside.chmod(0o644)
    directory = tmp_path / "private"
    directory.mkdir()
    os.link(outside, directory / name)
    with pytest.raises(ValueError, match="regular files"):
        PushStore(directory)
    assert outside.read_bytes() == b"must remain untouched"
    assert outside.stat().st_mode & 0o777 == 0o644


@pytest.mark.parametrize("name", ["push.lock", "push.sqlite3-wal", "push.sqlite3-journal"])
def test_symlink_sidecar_and_lock_refused_without_touching_target(tmp_path, name):
    outside = tmp_path / "outside"
    outside.write_bytes(b"unchanged")
    directory = tmp_path / "private"
    directory.mkdir()
    (directory / name).symlink_to(outside)
    with pytest.raises(ValueError):
        PushStore(directory)
    assert outside.read_bytes() == b"unchanged"
