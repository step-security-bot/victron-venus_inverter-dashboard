"""Encrypted Web Push with a bounded, public-provider-only HTTPS transport."""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import math
import re
import socket
import ssl
from urllib.parse import urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver

from .push_store import PushStore
from .push_subject import validate_push_subject

EXACT_PUSH_HOSTS = frozenset({"fcm.googleapis.com", "updates.push.services.mozilla.com"})
PUSH_SUFFIXES = (".push.apple.com", ".notify.windows.com")
IPV6_UNICAST = ipaddress.ip_network("2000::/3")
IPV6_SPECIAL = tuple(
    ipaddress.ip_network(value) for value in ("2001::/23", "2002::/16", "3fff::/20")
)


UNSUPPORTED_ENDPOINT = "Unsupported push endpoint"
INVALID_KEY = "Invalid push subscription key"


def endpoint_host(endpoint: str) -> str:
    """Endpoint authority is never allowed to select a local or arbitrary server."""
    if (
        not isinstance(endpoint, str)
        or len(endpoint) > 2048
        or any(ord(c) <= 32 or ord(c) >= 127 for c in endpoint)
        or "\\" in endpoint
    ):
        raise ValueError(UNSUPPORTED_ENDPOINT)
    try:
        parts = urlsplit(endpoint)
        host, port = parts.hostname, parts.port
    except ValueError:
        raise ValueError(UNSUPPORTED_ENDPOINT) from None
    if parts.username is not None or parts.password is not None:
        raise ValueError(UNSUPPORTED_ENDPOINT)
    if (
        parts.scheme != "https"
        or not host
        or port not in (None, 443)
        or not parts.path.startswith("/")
        or any((parts.fragment, host.endswith(".")))
    ):
        raise ValueError(UNSUPPORTED_ENDPOINT)
    if host not in EXACT_PUSH_HOSTS and not any(
        host.endswith(suffix) and re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)*", host[: -len(suffix)])
        for suffix in PUSH_SUFFIXES
    ):
        raise ValueError("Unsupported push provider")
    return host


def _decode_key(value: object, size: int) -> bytes:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+={0,2}", value):
        raise ValueError(INVALID_KEY)
    try:
        decoded = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except ValueError:
        raise ValueError(INVALID_KEY) from None
    if len(decoded) != size:
        raise ValueError(INVALID_KEY)
    return decoded


def validate_subscription(value: object) -> dict:
    """Return only the standard endpoint and encryption keys, without echoing errors."""
    from cryptography.hazmat.primitives.asymmetric import ec

    if not isinstance(value, dict):
        raise TypeError("Invalid push subscription")
    endpoint = value.get("endpoint")
    endpoint_host(endpoint)
    keys = value.get("keys")
    if not isinstance(keys, dict):
        raise TypeError("Invalid push subscription keys")
    public = _decode_key(keys.get("p256dh"), 65)
    _decode_key(keys.get("auth"), 16)
    try:
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), public)
    except ValueError:
        raise ValueError(INVALID_KEY) from None
    return {
        "endpoint": endpoint,
        "keys": {key: keys[key].rstrip("=") for key in ("p256dh", "auth")},
    }


class PublicPushResolver(AbstractResolver):
    """Validate DNS once and supply the same public IPs to the TLS connector."""

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_INET) -> list[dict]:
        endpoint_host(f"https://{host}/push")
        if port != 443:
            raise ValueError("Unsupported push port")
        loop = asyncio.get_running_loop()
        async with asyncio.timeout(3):
            answers = await loop.getaddrinfo(host, port, family=family, type=socket.SOCK_STREAM)
        if not answers or len(answers) > 32:
            raise ValueError("Push provider DNS unavailable")
        result = []
        for af, _, proto, _, address in answers:
            ip = ipaddress.ip_address(address[0])
            if not ip.is_global or ip.is_multicast or ip.is_unspecified:
                raise ValueError("Push provider DNS is not public")
            if ip.version == 6 and (
                ip not in IPV6_UNICAST or any(ip in network for network in IPV6_SPECIAL)
            ):
                raise ValueError("Push provider DNS is not public")
            result.append(
                {
                    "hostname": host,
                    "host": str(ip),
                    "port": port,
                    "family": af,
                    "proto": proto,
                    "flags": socket.AI_NUMERICHOST,
                }
            )
        return result

    async def close(self) -> None:
        return None


class PushTransport:
    """Library-owned RFC8291 encryption and RFC8292 VAPID; no payload or key logs."""

    def __init__(self, store: PushStore, subject: str):
        from py_vapid import Vapid

        self.subject = validate_push_subject(subject)
        raw = store.metadata("vapid_private_pem")
        if raw is None:
            if not store.new_database:
                raise ValueError("Existing push store has no VAPID key")
            key = Vapid()
            key.generate_keys()
            raw = store.initialize_metadata("vapid_private_pem", key.private_pem())
        self.vapid = Vapid.from_pem(raw)

    @property
    def public_key(self) -> str:
        from cryptography.hazmat.primitives import serialization

        public = self.vapid.public_key.public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
        return base64.urlsafe_b64encode(public).decode().rstrip("=")

    async def send(self, subscription: dict, payload: dict, now: float) -> int:
        from py_vapid.jwt import sign
        from pywebpush import WebPusher

        subscription = validate_subscription(subscription)
        host = endpoint_host(subscription["endpoint"])
        data = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
        if len(data) > 3072:
            raise ValueError("Push payload exceeds limit")
        source_ms = payload.get("sourceTimestampMs")
        if (
            (not isinstance(source_ms, int) or isinstance(source_ms, bool))
            or source_ms <= 0
            or source_ms / 1000 - now > 30
        ):
            raise ValueError("Invalid push event time")
        ttl = min(300, math.floor(source_ms / 1000 + 300 - now))
        if ttl <= 0:
            raise ValueError("Expired push event")
        encrypted = WebPusher(subscription).encode(data, content_encoding="aes128gcm")
        # py-vapid 1.9.4's convenience validator incorrectly rejects HTTPS paths.
        # Keep strict URI validation and fixed audience/expiry here; its public
        # signing primitive still owns JWT serialization and ES256 cryptography.
        token = sign(
            {
                "aud": f"https://{host}",
                "sub": validate_push_subject(self.subject),
                "exp": int(now) + 3600,
            },
            self.vapid.private_key,
        )
        headers = {"Authorization": f"vapid t={token},k={self.public_key}"}
        headers.update(
            {
                "Content-Encoding": "aes128gcm",
                "Content-Type": "application/octet-stream",
                "TTL": str(ttl),
                "Urgency": "normal",
            }
        )
        tls = ssl.create_default_context()
        tls.verify_mode = ssl.CERT_REQUIRED
        tls.check_hostname = True
        connector = aiohttp.TCPConnector(
            resolver=PublicPushResolver(),
            use_dns_cache=False,
            limit=1,
            ssl=tls,
            force_close=True,
        )
        timeout = aiohttp.ClientTimeout(total=10, connect=3, sock_read=3)
        async with (
            aiohttp.ClientSession(
                connector=connector,
                timeout=timeout,
                trust_env=False,
                auto_decompress=False,
                max_line_size=4096,
                max_field_size=4096,
            ) as session,
            session.post(
                subscription["endpoint"],
                data=encrypted["body"],
                headers=headers,
                allow_redirects=False,
            ) as response,
        ):
            # Status suffices. Never buffer/log a provider response body or Location.
            return response.status
