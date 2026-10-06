"""Strict RFC8292 contact URI validation, independent of optional crypto imports."""

import ipaddress
import re
from urllib.parse import unquote, urlsplit


def _contact_host(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return len(host) <= 253 and all(
            re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
            for label in host.split(".")
        )


def _unsafe_contact_text(value: str) -> bool:
    return bool(
        not isinstance(value, str)
        or not value
        or len(value) > 256
        or any(ord(char) <= 32 or ord(char) >= 127 for char in value)
        or "\\" in value
        or "#" in value
        or re.search(r"%(?![0-9A-Fa-f]{2})", value)
        or re.search(r"%(?:0[0-9A-Fa-f]|1[0-9A-Fa-f]|7[Ff])", value)
    )


def _https_contact(parsed, port: int | None) -> bool:
    return (
        bool(parsed.hostname)
        and _contact_host(parsed.hostname)
        and not parsed.netloc.endswith(":")
        and (port is None or 1 <= port <= 65535)
    )


def _mailto_contact(parsed) -> bool:
    local, separator, domain = parsed.path.partition("@")
    local = unquote(local)
    return (
        not parsed.netloc
        and not parsed.query
        and bool(separator)
        and bool(re.fullmatch(r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]+", local))
        and not local.startswith(".")
        and not local.endswith(".")
        and ".." not in local
        and "@" not in domain
        and ":" not in domain
        and _contact_host(domain)
    )


def validate_push_subject(value: str) -> str:
    """Accept an ASCII HTTPS contact URI or a single mailto address; never fetch it."""
    error = "WEB_PUSH_SUBJECT must be an HTTPS contact URI or mailto address"
    if _unsafe_contact_text(value):
        raise ValueError(error)
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError(error) from None
    if parsed.fragment or parsed.username is not None or parsed.password is not None:
        raise ValueError(error)
    if parsed.scheme == "https" and _https_contact(parsed, port):
        return value
    if parsed.scheme == "mailto" and _mailto_contact(parsed):
        return value
    raise ValueError(error)
