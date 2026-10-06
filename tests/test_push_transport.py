"""Endpoint, DNS, redirect and actual library encryption regression coverage."""

import asyncio
import base64
import json
import socket

import http_ece
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils

from inverter_dashboard import push_transport
from inverter_dashboard.config import Config
from inverter_dashboard.push_store import PushStore
from inverter_dashboard.push_transport import (
    PublicPushResolver,
    PushTransport,
    endpoint_host,
    validate_subscription,
)


def b64(data):
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def receiver():
    key = ec.generate_private_key(ec.SECP256R1())
    raw = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    auth = bytes(range(16))
    return (
        key,
        auth,
        {
            "endpoint": "https://fcm.googleapis.com/fcm/send/test",
            "keys": {"auth": b64(auth), "p256dh": b64(raw)},
        },
    )


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://fcm.googleapis.com/x",
        "https://127.0.0.1/x",
        "https://[::1]/x",
        "https://fcm.googleapis.com.evil.test/x",
        "https://fcm.googleapis.com:444/x",
        "https://user@fcm.googleapis.com/x",
        "https://fcm.googleapis.com/x#secret",
        "https://fcm.googleapis.com./x",
        "https://fcm.googleapis.com\\@localhost/x",
        "https://push.apple.com/x",
        "https://evilnotify.windows.com/x",
        "https://evil.test/x",
        "https://fcm.googleapis.com/\n",
    ],
)
def test_arbitrary_or_ambiguous_endpoints_rejected(endpoint):
    with pytest.raises(ValueError):
        endpoint_host(endpoint)


@pytest.mark.parametrize(
    "host",
    [
        "fcm.googleapis.com",
        "updates.push.services.mozilla.com",
        "web.push.apple.com",
        "wns2-db5p.notify.windows.com",
    ],
)
def test_known_provider_authorities(host):
    assert endpoint_host(f"https://{host}/endpoint?token=opaque") == host


def test_subscription_keys_validate_curve_and_length():
    _, _, subscription = receiver()
    assert validate_subscription(subscription) == subscription
    for field, bad in [("auth", "eA"), ("p256dh", b64(b"\x04" + b"\0" * 64))]:
        invalid = {**subscription, "keys": {**subscription["keys"], field: bad}}
        with pytest.raises(ValueError):
            validate_subscription(invalid)


@pytest.mark.parametrize(
    "addresses",
    [
        ["127.0.0.1"],
        ["10.0.0.1"],
        ["169.254.169.254"],
        ["8.8.8.8", "192.168.1.1"],
        ["224.0.0.1"],
        ["64:ff9b::a00:1"],
        ["2002:a00:1::"],
        ["2001::1"],
        ["::ffff:127.0.0.1"],
        ["3fff::1"],
        ["2001:4860:4860::8888", "64:ff9b::a00:1"],
    ],
)
async def test_dns_rebinding_to_private_or_mixed_addresses_rejected(monkeypatch, addresses):
    async def resolve(*_args, **_kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443)) for ip in addresses]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    with pytest.raises(ValueError, match="not public"):
        await PublicPushResolver().resolve("fcm.googleapis.com", 443)


async def test_resolver_supplies_validated_ip_to_connector(monkeypatch):
    calls = []

    async def resolve(*args, **_kwargs):
        calls.append(args)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    addresses = await PublicPushResolver().resolve("fcm.googleapis.com", 443)
    assert len(calls) == 1
    assert addresses[0]["host"] == "8.8.8.8"
    assert addresses[0]["hostname"] == "fcm.googleapis.com"
    assert addresses[0]["flags"] == socket.AI_NUMERICHOST


@pytest.mark.parametrize(
    "subject",
    [
        Config.model_fields["WEB_PUSH_SUBJECT"].default,
        "mailto:notifications@example.com",
        "https://example.com:8443/contact/team?project=dashboard",
    ],
)
async def test_real_encryption_and_hardened_transport_without_network(
    tmp_path, monkeypatch, subject
):
    captured = {}

    class Response:
        """Minimal provider response, intentionally without a body reader."""

        status = 201

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    class Session(Response):
        """Capture encrypted request instead of sending it."""

        def __init__(self, **kwargs):
            captured["session"] = kwargs

        def post(self, endpoint, **kwargs):
            captured["request"] = kwargs
            captured["endpoint"] = endpoint
            return Response()

        async def __aexit__(self, *_args):
            await captured["session"]["connector"].close()
            return False

    monkeypatch.setattr(push_transport.aiohttp, "ClientSession", Session)
    key, auth, subscription = receiver()
    store = PushStore(tmp_path)
    transport = PushTransport(store, subject)
    original_public = transport.public_key
    assert PushTransport(store, subject).public_key == original_public
    payload = {
        "title": "Battery alarm",
        "body": "Original event",
        "sourceTimestampMs": 1700000000000,
    }
    assert await transport.send(subscription, payload, 1700000001) == 201
    sent = captured["request"]
    assert sent["allow_redirects"] is False
    assert captured["session"]["trust_env"] is False
    assert captured["session"]["timeout"].total == 10
    assert sent["headers"]["Content-Encoding"] == "aes128gcm"
    assert sent["headers"]["TTL"] == "299"
    assert sent["headers"]["Authorization"].startswith("vapid ")
    token, public = sent["headers"]["Authorization"].removeprefix("vapid t=").split(",k=")
    header, claims, signature = token.split(".")

    def decode(value):
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))

    assert json.loads(decode(header)) == {"typ": "JWT", "alg": "ES256"}
    assert json.loads(decode(claims)) == {
        "aud": "https://fcm.googleapis.com",
        "sub": subject,
        "exp": 1700003601,
    }
    assert public == original_public
    raw_signature = decode(signature)
    assert len(raw_signature) == 64
    der_signature = utils.encode_dss_signature(
        int.from_bytes(raw_signature[:32], "big"), int.from_bytes(raw_signature[32:], "big")
    )
    signing_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), decode(public))
    signing_key.verify(der_signature, f"{header}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))
    assert transport.vapid.conf == {}  # No no-strict/global library validation switch.
    assert b"Battery alarm" not in sent["data"]
    clear = http_ece.decrypt(sent["data"], private_key=key, auth_secret=auth, version="aes128gcm")
    assert json.loads(clear) == payload
    store.close()


@pytest.mark.parametrize("raw", [None, b"not-a-private-key"])
def test_existing_store_cannot_silently_regenerate_missing_or_corrupt_key(tmp_path, raw):
    store = PushStore(tmp_path)
    if raw is not None:
        store.initialize_metadata("vapid_private_pem", raw)
    store.close()
    store = PushStore(tmp_path)
    with pytest.raises(ValueError):
        PushTransport(store, "https://github.com/victron-venus/inverter-dashboard")
    store.close()


@pytest.mark.parametrize("source", [1700000000000, 1700000340000, None, True])
async def test_expired_future_or_invalid_event_refused_before_http(tmp_path, monkeypatch, source):
    store = PushStore(tmp_path)
    transport = PushTransport(store, "https://github.com/victron-venus/inverter-dashboard")

    def no_http(**_kwargs):
        raise AssertionError("invalid timestamp reached outbound HTTP")

    monkeypatch.setattr(push_transport.aiohttp, "ClientSession", no_http)
    with pytest.raises(ValueError):
        await transport.send(receiver()[2], {"sourceTimestampMs": source}, 1700000301)
    store.close()
