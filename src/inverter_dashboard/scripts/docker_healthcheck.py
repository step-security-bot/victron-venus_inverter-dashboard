#!/usr/bin/env python3
"""Docker HEALTHCHECK: HTTP or HTTPS when dashboard.crt + dashboard.key exist in config."""

from __future__ import annotations

import http.client
import os
import ssl


def main() -> int:
    config = os.environ.get("INVERTER_DASHBOARD_CONFIG", "/app/config")
    crt = os.path.join(config, "dashboard.crt")
    key = os.path.join(config, "dashboard.key")
    timeout = 8

    connection = None
    try:
        port = int(os.environ.get("WEB_PORT", "8080"))
        if not 1 <= port <= 65535:
            return 1
        if os.path.isfile(crt) and os.path.isfile(key):
            # For HTTPS with self-signed certs inside container at localhost:
            # trust the dashboard's own cert file instead of disabling verification.
            # Hostname verification stays enabled (ssl.PROTOCOL_TLS_CLIENT default);
            # scripts/ssl-local-deploy.sh always issues dashboard.crt with 127.0.0.1
            # as an IP SAN, so the handshake against https://127.0.0.1 succeeds.
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.load_verify_locations(cafile=crt)
            connection = http.client.HTTPSConnection(
                "127.0.0.1", port, context=ctx, timeout=timeout
            )
        else:
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        # A health probe must stay on loopback, including when the server redirects.
        connection.request("GET", "/api/state")
        status = connection.getresponse().status
        # 401/403 means the server is up but requires authentication.
        return 0 if 200 <= status < 300 or status in (401, 403) else 1
    except (OSError, ValueError, http.client.HTTPException):
        return 1
    finally:
        if connection is not None:
            connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
