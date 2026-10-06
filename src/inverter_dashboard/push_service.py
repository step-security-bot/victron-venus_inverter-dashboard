"""Opt-in Web Push lifecycle and durable delivery independent of browser sessions."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import platform
import sqlite3
import time
import uuid
from collections import deque
from pathlib import Path

from .push_events import (
    DEFAULT_PREFERENCES,
    EventProcessor,
    event_payload,
    preferences,
    validate_persisted_payload,
)
from .push_store import PushStore
from .push_transport import PushTransport, endpoint_host, validate_subscription

logger = logging.getLogger(__name__)


def _failure_category(error: Exception) -> str:
    """Fixed labels only: exception text can contain private endpoint capabilities."""
    if isinstance(error, TimeoutError):
        return "timeout"
    if isinstance(error, ImportError):
        return "dependency"
    return {
        "ClientConnectorCertificateError": "tls",
        "SSLCertVerificationError": "tls",
        "ClientConnectorDNSError": "dns",
        "gaierror": "dns",
        "ClientConnectorError": "connection",
        "ClientOSError": "connection",
        "ConnectionResetError": "connection",
        "ServerDisconnectedError": "connection",
        "VapidException": "vapid",
    }.get(type(error).__name__, "unexpected")


def _delivery_log_context(delivery: dict) -> tuple[str, str]:
    kind = delivery["payload"].get("kind")
    if not isinstance(kind, str) or kind not in (*DEFAULT_PREFERENCES, "test"):
        kind = "unknown"
    attempt = delivery.get("attempts")
    valid = isinstance(attempt, int) and not isinstance(attempt, bool) and 0 <= attempt <= 2
    return kind, str(int(attempt) + 1) if valid else "unknown"


def _log_provider_status(status: int, kind: str, attempt: str) -> None:
    if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599:
        logger.info(
            "Web Push provider response status=%d kind=%s attempt=%s", int(status), kind, attempt
        )
    else:
        logger.warning(
            "Web Push delivery failed category=invalid_status kind=%s attempt=%s", kind, attempt
        )


class PushRateLimit(ValueError):
    """Bounded subscription/test capacity reached."""


class PushService:
    """Single writer, at most two sends, no subscription/private-key diagnostics."""

    def __init__(self, directory: Path, subject: str):
        self.available = False
        self.unavailable_reason = "storage_unavailable"
        self.store = None
        self.transport = None
        self.processor = None
        self.connection: tuple[str, object] | None = None
        self.workers: list[asyncio.Task] = []
        self.inflight: dict[asyncio.Task, tuple[str, str]] = {}
        self.test_times: deque[float] = deque()
        # Current cryptography has no Intel macOS wheels. The optional sender
        # must not prevent the dashboard from importing or starting there.
        if platform.system() == "Windows" or (
            platform.system() == "Darwin" and platform.machine() == "x86_64"
        ):
            self.unavailable_reason = "unsupported_platform"
            return
        try:
            self.store = PushStore(directory)
            self.transport = PushTransport(self.store, subject)
            for item in self.store.subscriptions():
                validated = validate_subscription(item["subscription"])
                if (
                    validated != item["subscription"]
                    or hashlib.sha256(validated["endpoint"].encode()).hexdigest() != item["id"]
                ):
                    raise ValueError("Push store subscription identity mismatch")
                preferences(item["preferences"])
            for event_id, payload in self.store.queued_payloads():
                validate_persisted_payload(payload)
                if payload["eventKey"] != event_id:
                    raise ValueError("Push store delivery identity mismatch")
            self.store.mark_initialized()
            self.processor = EventProcessor(self.store)
            self.processor.reset(uuid.uuid4().hex, time.time())
            self.available = True
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
            if self.store is not None:
                self.store.close()
                self.store = None
            logger.error("Web Push storage unavailable; notification delivery disabled")

    def start(self) -> None:
        if not self.available:
            return
        self.workers = [asyncio.create_task(self._worker()) for _ in range(2)]

    async def close(self) -> None:
        tasks = [*self.workers, *self.inflight]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.workers.clear()
        self.inflight.clear()
        if self.store is not None:
            self.store.close()

    def connect(self, source: str, owner: object, *, force: bool = False) -> bool:
        if not self.available:
            return False
        if not force and self.connection == (source, owner):
            return False
        try:
            self._retire()
        except (sqlite3.Error, OSError):
            self.fail()
            return False
        self.connection = (source, owner)
        if source == "mqtt":
            self.processor.native([], time.time(), prime=True)
        return True

    def disconnect(self, source: str | None = None, owner: object | None = None) -> None:
        if not self.available:
            return
        if source is not None and self.connection != (source, owner):
            return
        self.connection = None
        try:
            self._retire()
        except (sqlite3.Error, OSError):
            self.fail()

    def _retire(self) -> None:
        for task, (epoch, _) in self.inflight.items():
            if epoch != "test":
                task.cancel()
        self.processor.reset(uuid.uuid4().hex, time.time())

    def registered(self, endpoint: str) -> dict | None:
        endpoint_host(endpoint)
        return self.store.get(hashlib.sha256(endpoint.encode()).hexdigest())

    def register(self, subscription: dict, selected: dict) -> dict:
        validated = validate_subscription(subscription)
        selected = preferences(selected)
        try:
            identifier = self.store.register(validated, selected, time.time())
        except ValueError as exc:
            if "limit" in str(exc):
                raise PushRateLimit("Push subscription limit reached") from None
            raise
        self._cancel_subscription(identifier)
        return selected

    def _cancel_subscription(self, identifier: str) -> None:
        for task, (_, subscription_id) in self.inflight.items():
            if subscription_id == identifier:
                task.cancel()

    def delete(self, endpoint: str) -> None:
        endpoint_host(endpoint)
        identifier = hashlib.sha256(endpoint.encode()).hexdigest()
        self._cancel_subscription(identifier)
        self.store.delete(identifier)

    def test(self, endpoint: str) -> None:
        subscription = self.registered(endpoint)
        if subscription is None:
            raise KeyError("Subscription not registered")
        now = time.time()
        while self.test_times and now - self.test_times[0] >= 60:
            self.test_times.popleft()
        if (
            not self.store.has_delivery_capacity(now)
            or len(self.test_times) >= 10
            or not self.store.allow_test(subscription["id"], now)
        ):
            raise PushRateLimit("Notification test rate limit reached")
        self.test_times.append(now)
        event = event_payload(
            "test",
            "system",
            uuid.uuid4().hex,
            int(now * 1000),
            now,
            title="Inverter Dashboard test",
            body="This is a requested test notification.",
        )
        self.store.enqueue(event["eventKey"], event, [subscription["id"]], now, "test")

    def queue(self, event: dict | None) -> None:
        if not self.available or event is None or self.connection is None:
            return
        targets = [
            item["id"]
            for item in self.store.subscriptions()
            if item["preferences"].get(event["kind"]) is True
        ]
        self.store.enqueue(event["eventKey"], event, targets, time.time(), self.processor.epoch)

    def fail(self) -> None:
        """Optional push storage must fail closed without stopping telemetry."""
        self.available = False
        self.connection = None
        for task in (*self.workers, *self.inflight):
            if task is not asyncio.current_task():
                task.cancel()
        logger.error("Web Push storage unavailable; notification delivery disabled")

    async def _worker(self) -> None:
        try:
            while self.available:
                delivery = self.store.next_delivery(time.time(), claim=True)
                if delivery is None:
                    await asyncio.sleep(0.5)
                    continue
                await self._deliver(delivery)
        except (sqlite3.Error, OSError, ValueError, KeyError, TypeError):
            self.fail()

    async def _deliver(self, delivery: dict) -> None:
        log_kind, log_attempt = _delivery_log_context(delivery)
        epoch = delivery["epoch"]
        subscription = self.store.get(delivery["subscription_id"])
        kind = delivery["payload"]["kind"]
        if (
            subscription is None
            or epoch not in ("test", self.processor.epoch)
            or (kind != "test" and not subscription["preferences"].get(kind))
        ):
            self.store.finish(delivery)
            return
        task = asyncio.create_task(
            self.transport.send(subscription["subscription"], delivery["payload"], time.time())
        )
        self.inflight[task] = (epoch, subscription["id"])
        retry_at = None
        try:
            status = await task
            if not self._owns_delivery(delivery):
                return
            _log_provider_status(status, log_kind, log_attempt)
            if status in (404, 410):
                self.store.delete(subscription["id"])
            elif status == 429 or status >= 500:
                retry_at = time.time() + min(60, 5 * 2 ** delivery["attempts"])
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                raise
        except (ValueError, TypeError):
            logger.warning(
                "Web Push delivery failed category=validation kind=%s attempt=%s",
                log_kind,
                log_attempt,
            )
        except Exception as error:
            logger.warning(
                "Web Push delivery failed category=%s kind=%s attempt=%s",
                _failure_category(error),
                log_kind,
                log_attempt,
            )
            retry_at = time.time() + min(60, 5 * 2 ** delivery["attempts"])
        finally:
            self.inflight.pop(task, None)
        if self._owns_delivery(delivery):
            self.store.finish(delivery, retry_at)

    def _owns_delivery(self, delivery: dict) -> bool:
        return (
            self.available
            and delivery["epoch"] in ("test", self.processor.epoch)
            and self.store.owns_delivery(delivery, time.time())
        )

    def status(self) -> dict:
        return {
            "enabled": True,
            "available": self.available,
            "reason": None if self.available else self.unavailable_reason,
            "publicKey": self.transport.public_key if self.available else None,
            "preferencesDefaults": dict(DEFAULT_PREFERENCES),
            "maxNotificationAgeSeconds": 300,
        }
