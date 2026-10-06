"""Occurrence time, reconnect priming and transition freshness regressions."""

import hashlib
import json
from datetime import UTC, datetime

import pytest

from inverter_dashboard.push_events import EventProcessor, event_payload, timestamp_ms
from inverter_dashboard.push_store import PushStore

NOW = 1_800_000_000.0


def native(identifier="slot:1", when=NOW, **updates):
    return {
        "id": identifier,
        "source": "victron",
        "level": "alarm",
        "title": "Low battery",
        "body": "Battery one",
        "ts": datetime.fromtimestamp(when, UTC).isoformat(),
        **updates,
    }


@pytest.fixture
def processor(tmp_path):
    store = PushStore(tmp_path)
    result = EventProcessor(store)
    result.reset("epoch-1", NOW)
    yield result
    store.close()


def test_first_snapshot_and_reconnect_never_replay_current_native_alerts(processor):
    alarm = native()
    assert processor.native([alarm], NOW) == []
    candidates = processor.native([alarm], NOW + 1)
    assert len(candidates) == 1
    assert not processor.store.enqueue(candidates[0]["eventKey"], candidates[0], [], NOW + 1)
    processor.reset("epoch-2", NOW + 2)
    assert processor.native([alarm], NOW + 2) == []
    assert processor.samples == {}


def test_unknown_initial_time_hydration_is_silent_but_reused_cleared_slot_is_new(processor):
    assert processor.native([native(ts=None)], NOW) == []
    assert processor.native([native(when=NOW + 10)], NOW + 10) == []
    processor.native([], NOW + 11)
    assert processor.native([native(when=NOW + 12)], NOW + 12)[0]["sourceTimestampMs"] == int(
        (NOW + 12) * 1000
    )
    processor.reset("new-epoch", NOW + 13)
    processor.native([native(ts=None)], NOW + 13)
    processor.native([], NOW + 14)
    assert processor.native([native(when=NOW + 15)], NOW + 15)


def test_unknown_slot_replaced_with_different_occurrence_does_not_swallow_it(processor):
    processor.native([native(ts=None)], NOW)
    events = processor.native([native(when=NOW + 1, title="High temperature")], NOW + 1)
    assert len(events) == 1 and events[0]["title"] == "High temperature"


@pytest.mark.parametrize(
    "updates",
    [
        {"ts": None},
        {"ts": "2027-01-15T08:00:00"},
        {"when": NOW - 31},
        {"when": NOW + 32},
        {"level": "info"},
    ],
)
def test_unknown_stale_future_and_nonwarning_native_events_do_not_notify(processor, updates):
    processor.native([], NOW)
    assert processor.native([native(**updates)], NOW + 1) == []


def test_original_time_preserved_and_age_limit_enforced_even_late_in_epoch(processor):
    processor.native([], NOW)
    event = processor.native([native(when=NOW + 10)], NOW + 250)[0]
    assert event["sourceTimestampMs"] == int((NOW + 10) * 1000)
    assert event["observedAtMs"] == int((NOW + 250) * 1000)
    assert processor.native([native(when=NOW + 10)], NOW + 311) == []


def test_payload_unicode_hash_and_byte_bounds_match_shared_contract():
    payload = event_payload(
        "native", "victron", "警告", int(NOW * 1000), NOW, title="警" * 200, body="🙂" * 2000
    )
    identity = json.dumps(
        ["native", "victron", "警告", int(NOW * 1000)], ensure_ascii=False, separators=(",", ":")
    ).encode()
    assert payload["eventKey"] == hashlib.sha256(identity).hexdigest()
    assert len(payload["title"]) == 120
    assert len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()) <= 3072
    assert timestamp_ms("2027-01-15T08:00:00Z") == timestamp_ms("2027-01-15T10:00:00+02:00")


@pytest.mark.parametrize(
    "kind,identifier,before,after",
    [
        ("ev", "evcharger:1", False, True),
        ("water", "pump:1", True, False),
        ("water", "valve:2", False, True),
        ("lowBattery", "battery:1", 21, 20),
    ],
)
def test_synthetic_fresh_same_epoch_transition_only(processor, kind, identifier, before, after):
    assert processor.sample(kind, identifier, before, NOW) is None
    event = processor.sample(kind, identifier, after, NOW + 1)
    assert event and event["kind"] == kind
    assert event["sourceTimestampMs"] == int((NOW + 1) * 1000)
    assert processor.sample(kind, identifier, after, NOW + 2) is None
    processor.reset("other", NOW + 3)
    assert processor.sample(kind, identifier, before, NOW + 3) is None


def test_unknown_stale_and_prime_synthetic_samples_cannot_form_transition(processor):
    processor.sample("ev", "evcharger:1", False, NOW)
    assert processor.sample("ev", "evcharger:1", True, NOW + 31) is None
    processor.sample("ev", "evcharger:1", None, NOW + 32)
    assert processor.sample("ev", "evcharger:1", False, NOW + 33) is None
    assert processor.sample("ev", "evcharger:1", True, NOW + 34, prime=True) is None
    assert processor.sample("lowBattery", "system:0", 10, NOW) is None
    assert processor.sample("lowBattery", "system:0", 9, NOW + 1) is None
    assert processor.sample("lowBattery", "system:0", 21, NOW + 2) is None
    assert processor.sample("lowBattery", "system:0", 19, NOW + 3)


def test_backward_wall_clock_cannot_make_future_baseline_fresh(processor):
    assert processor.sample("ev", "charger:1", False, NOW + 10) is None
    assert processor.sample("ev", "charger:1", True, NOW) is None
    assert processor.sample("ev", "charger:1", False, NOW + 1) is not None


@pytest.mark.parametrize(
    "changes",
    [
        {"schemaVersion": True},
        {"schemaVersion": 1.0},
        {"eventKey": "not-a-hash"},
        {"kind": "grid"},
        {"source": "unknown"},
        {"url": "https://other.test/"},
        {"sourceTimestampMs": True},
        {"observedAtMs": 10**30},
        {"title": []},
        {"body": "x" * 1001},
        {"body": "🙂" * 1000},
    ],
)
def test_persisted_payload_invalid_field_types_and_bounds_are_rejected(changes):
    from inverter_dashboard.push_events import validate_persisted_payload

    event = event_payload(
        "native", "victron", "alarm", int(NOW * 1000), NOW, title="Alarm", body="Native condition"
    )
    validate_persisted_payload(event)
    with pytest.raises((ValueError, TypeError)):
        validate_persisted_payload({**event, **changes})
