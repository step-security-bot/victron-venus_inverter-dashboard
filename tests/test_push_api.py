"""Production API contracts with real storage and crypto, without outbound sends."""

import base64

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from inverter_dashboard import server
from inverter_dashboard.push_api import install_push_api
from inverter_dashboard.push_events import DEFAULT_PREFERENCES
from inverter_dashboard.push_service import PushService


def subscription():
    key = ec.generate_private_key(ec.SECP256R1())
    public = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )

    def encode(data):
        return base64.urlsafe_b64encode(data).decode().rstrip("=")

    return {
        "endpoint": "https://fcm.googleapis.com/fcm/send/opaque-token",
        "keys": {"p256dh": encode(public), "auth": encode(bytes(range(16)))},
    }


@pytest.fixture
async def api(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DASHBOARD_SECRET", "test-bearer")
    service = PushService(tmp_path, "https://github.com/victron-venus/inverter-dashboard")
    app = FastAPI()
    install_push_api(app, lambda: service, server._verify_secret)
    client = AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://dashboard.test",
        headers={"Authorization": "Bearer test-bearer", "Origin": "https://dashboard.test"},
    )
    yield (client, service)
    await client.aclose()
    service.store.close()


async def test_subscribe_status_update_conflict_delete(api):
    client, service = api
    sub = subscription()
    status = await client.get("/api/notifications/status")
    assert status.status_code == 200
    assert status.json()["available"] is True
    assert len(status.json()["publicKey"]) == 87
    assert status.headers["cache-control"] == "no-store"
    body = {"subscription": sub, "preferences": dict(DEFAULT_PREFERENCES)}
    assert (await client.post("/api/notifications/subscription", json=body)).json()[
        "registered"
    ] is True
    body["preferences"]["water"] = False
    assert (await client.post("/api/notifications/subscription", json=body)).json()["preferences"][
        "water"
    ] is False
    changed = subscription()
    assert (
        await client.post("/api/notifications/subscription", json={**body, "subscription": changed})
    ).status_code == 409
    lookup = {"endpoint": sub["endpoint"]}
    assert (await client.post("/api/notifications/subscription/status", json=lookup)).json()[
        "preferences"
    ]["water"] is False
    assert (
        await client.request("DELETE", "/api/notifications/subscription", json=lookup)
    ).json() == {"registered": False}
    assert (
        await client.request("DELETE", "/api/notifications/subscription", json=lookup)
    ).status_code == 200
    assert (await client.post("/api/notifications/subscription/status", json=lookup)).json() == {
        "registered": False,
        "preferences": DEFAULT_PREFERENCES,
    }
    assert service.store.count() == 0


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "https://evil.test"},
        {"Origin": "null"},
        {"Origin": ""},
        {"Origin": "http://dashboard.test"},
        {"Origin": "https://dashboard.test/"},
        {"Origin": "https://dashboard.test", "Sec-Fetch-Site": "cross-site"},
        {
            "Origin": "https://evil.test",
            "X-Forwarded-Host": "evil.test",
            "X-Forwarded-Proto": "https",
        },
        {"Origin": "https://user@dashboard.test"},
    ],
)
async def test_origin_cannot_be_forged_with_proxy_headers(api, headers):
    client, service = api
    response = await client.post(
        "/api/notifications/subscription/status",
        json={"endpoint": subscription()["endpoint"]},
        headers=headers,
    )
    assert response.status_code == 403
    assert response.headers["cache-control"] == "no-store"
    assert service.store.count() == 0


async def test_auth_applies_to_all_routes_and_query_token_is_not_accepted(api):
    client, _ = api
    for method, path in [
        ("GET", "/status"),
        ("POST", "/subscription/status"),
        ("POST", "/subscription"),
        ("DELETE", "/subscription"),
        ("POST", "/test"),
    ]:
        response = await client.request(
            method,
            f"/api/notifications{path}?token=test-bearer",
            json={},
            headers={"Authorization": ""},
        )
        assert response.status_code == 401


async def test_bounded_json_preferences_and_endpoint_validation(api):
    client, service = api
    path = "/api/notifications/subscription"
    assert (
        await client.post(path, content="x", headers={"Content-Type": "text/plain"})
    ).status_code == 415
    assert (
        await client.post(path, content="{", headers={"Content-Type": "application/json"})
    ).status_code == 400
    assert (
        await client.post(path, content="x" * 8193, headers={"Content-Type": "application/json"})
    ).status_code == 413
    assert (await client.post(path, json=[])).status_code == 400
    body = {"subscription": subscription(), "preferences": {**DEFAULT_PREFERENCES, "native": 1}}
    assert (await client.post(path, json=body)).status_code == 400
    body["preferences"] = dict(DEFAULT_PREFERENCES)
    body["subscription"]["endpoint"] = "https://127.0.0.1/private?credential=never-echo"
    response = await client.post(path, json=body)
    assert response.status_code == 400
    assert "never-echo" not in response.text
    assert service.store.count() == 0


async def test_test_notification_is_only_queued_and_rate_limited(api):
    client, service = api
    sub = subscription()
    body = {"endpoint": sub["endpoint"]}
    assert (await client.post("/api/notifications/test", json=body)).status_code == 404
    service.register(sub, dict(DEFAULT_PREFERENCES))
    response = await client.post("/api/notifications/test", json=body)
    assert response.status_code == 202 and response.json() == {"queued": True}
    assert (await client.post("/api/notifications/test", json=body)).status_code == 429
    assert not service.workers and (not service.inflight)
    import time

    assert service.store.next_delivery(time.time())["payload"]["kind"] == "test"


async def test_disabled_status_is_explicit_and_mutations_fail_closed(monkeypatch):
    monkeypatch.setattr(server, "DASHBOARD_SECRET", "")
    app = FastAPI()
    install_push_api(app, lambda: None, server._verify_secret)
    client = AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://dashboard.test",
        headers={"Origin": "https://dashboard.test"},
    )
    assert (await client.get("/api/notifications/status")).json()["available"] is False
    assert (
        await client.post("/api/notifications/test", json={"endpoint": "opaque"})
    ).status_code == 503

    await client.aclose()


async def test_chunked_body_limit_applies_before_json_decoding(api):
    client, _ = api

    async def chunks():
        yield b"{" + b" " * 4000
        yield b" " * 5000

    response = await client.post(
        "/api/notifications/subscription",
        content=chunks(),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413
    assert response.headers["cache-control"] == "no-store"


async def test_storage_failure_reports_unavailable_without_leaking_detail(api, monkeypatch):
    import sqlite3

    client, service = api

    def broken(_endpoint):
        raise sqlite3.DatabaseError("private endpoint must never appear")

    monkeypatch.setattr(service, "registered", broken)
    response = await client.post(
        "/api/notifications/subscription/status", json={"endpoint": subscription()["endpoint"]}
    )
    assert response.status_code == 503
    assert "private endpoint" not in response.text
    assert response.headers["cache-control"] == "no-store"
    status = (await client.get("/api/notifications/status")).json()
    assert status["enabled"] is True and status["available"] is False
    assert status["publicKey"] is None and status["reason"] == "storage_unavailable"
    assert (
        await client.post("/api/notifications/test", json={"endpoint": subscription()["endpoint"]})
    ).status_code == 503


async def test_browser_subscription_expiration_time_null_is_accepted(api):
    client, _ = api
    sub = {**subscription(), "expirationTime": None}
    response = await client.post(
        "/api/notifications/subscription",
        json={"subscription": sub, "preferences": DEFAULT_PREFERENCES},
    )
    assert response.status_code == 200 and response.json()["registered"] is True


def test_openapi_documents_notification_error_contract():
    from fastapi import FastAPI

    from inverter_dashboard.push_api import ERROR_RESPONSES, install_push_api

    app = FastAPI()
    install_push_api(app, lambda: None, lambda request: None)
    schema = app.openapi()
    for path, methods in schema["paths"].items():
        assert path.startswith("/api/notifications/")
        for operation in methods.values():
            assert {str(code) for code in ERROR_RESPONSES} <= set(operation["responses"])
    assert "202" in schema["paths"]["/api/notifications/test"]["post"]["responses"]
