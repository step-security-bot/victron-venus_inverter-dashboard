"""Health probes never leave loopback or disable certificate verification."""

import ssl
from types import SimpleNamespace

import pytest
import trustme

from inverter_dashboard.scripts import docker_healthcheck


class Connection:
    def __init__(self, status):
        self.status = status
        self.requests = []
        self.closed = False

    def request(self, method, path):
        self.requests.append((method, path))

    def getresponse(self):
        return SimpleNamespace(status=self.status)

    def close(self):
        self.closed = True


@pytest.mark.parametrize("status", [200, 204, 301, 302, 401, 403, 404, 500])
def test_loopback_probe_never_follows_redirects(monkeypatch, tmp_path, status):
    monkeypatch.setenv("INVERTER_DASHBOARD_CONFIG", str(tmp_path))
    monkeypatch.setenv("WEB_PORT", "8123")
    connection = Connection(status)

    def connect(host, port, *, timeout):
        assert (host, port, timeout) == ("127.0.0.1", 8123, 8)
        return connection

    monkeypatch.setattr(docker_healthcheck.http.client, "HTTPConnection", connect)
    assert docker_healthcheck.main() == (0 if status in (200, 204, 401, 403) else 1)
    assert connection.requests == [("GET", "/api/state")]
    assert connection.closed


@pytest.mark.parametrize("port", ["0", "-1", "65536", "8080@external.invalid", "file:///etc/hosts"])
def test_rejects_invalid_port_before_connecting(monkeypatch, port):
    monkeypatch.setenv("WEB_PORT", port)

    def unexpected(*args, **kwargs):
        pytest.fail("invalid port must not create a connection")

    monkeypatch.setattr(docker_healthcheck.http.client, "HTTPConnection", unexpected)
    assert docker_healthcheck.main() == 1


def test_https_keeps_certificate_and_hostname_verification(monkeypatch, tmp_path):
    monkeypatch.setenv("INVERTER_DASHBOARD_CONFIG", str(tmp_path))
    monkeypatch.setenv("WEB_PORT", "8443")
    ca = trustme.CA()
    ca.cert_pem.write_to_path(tmp_path / "dashboard.crt")
    (tmp_path / "dashboard.key").touch()
    connection = Connection(200)

    def connect(host, port, *, context, timeout):
        assert (host, port, timeout) == ("127.0.0.1", 8443, 8)
        assert context.check_hostname
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.cert_store_stats()["x509_ca"] == 1
        return connection

    monkeypatch.setattr(docker_healthcheck.http.client, "HTTPSConnection", connect)
    assert docker_healthcheck.main() == 0
    assert connection.closed


def test_failed_request_closes_connection(monkeypatch, tmp_path):
    monkeypatch.setenv("INVERTER_DASHBOARD_CONFIG", str(tmp_path))
    connection = Connection(200)

    def fail(*args):
        raise OSError("connection failed")

    connection.request = fail
    monkeypatch.setattr(
        docker_healthcheck.http.client, "HTTPConnection", lambda *a, **k: connection
    )
    assert docker_healthcheck.main() == 1
    assert connection.closed
