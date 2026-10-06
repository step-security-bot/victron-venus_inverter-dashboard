"""Unsupported optional senders must not break packaged dashboard startup."""

import json
import os
import subprocess
import sys
import textwrap
import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement

from inverter_dashboard import push_service


@pytest.mark.parametrize(
    ("system", "machine", "excluded"),
    [
        ("Darwin", "x86_64", True),
        ("Darwin", "arm64", False),
        ("Linux", "x86_64", False),
        ("Linux", "aarch64", False),
        ("Windows", "AMD64", False),
    ],
)
def test_optional_crypto_dependency_markers_match_release_targets(system, machine, excluded):
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    requirements = {r.name: r for r in map(Requirement, project["project"]["dependencies"])}
    environment = {
        "sys_platform": {"Darwin": "darwin", "Linux": "linux", "Windows": "win32"}[system],
        "platform_machine": machine,
    }
    for name in ("pywebpush", "cryptography", "py-vapid", "http-ece"):
        assert requirements[name].marker.evaluate(environment) is not excluded
    assert str(requirements["cryptography"].specifier) == ">=50.0.1"


@pytest.mark.parametrize("system", ["Darwin", "Windows"])
def test_no_crypto_imports_or_store_writes_during_unsupported_dashboard_startup(tmp_path, system):
    # A fresh interpreter prevents already-imported test dependencies from hiding
    # an eager crypto import. Run the real lifespan/API with only telemetry I/O stubbed.
    script = textwrap.dedent(
        """
        import asyncio, importlib.abc, json, pathlib, sys
        denied = []
        class NoOptionalCrypto(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname.split('.')[0] in {'cryptography', 'py_vapid', 'pywebpush', 'http_ece'}:
                    denied.append(fullname)
                    raise ModuleNotFoundError('Optional sender dependency is absent')
                return None
        sys.meta_path.insert(0, NoOptionalCrypto())
        from httpx import ASGITransport, AsyncClient
        from inverter_dashboard import server, push_service
        push_service.platform.system = lambda: sys.argv[1]
        push_service.platform.machine = lambda: 'x86_64'
        directory = pathlib.Path('unused-push-store')
        server.config.WEB_PUSH_ENABLED = True
        server.config.WEB_PUSH_DATA_DIR = str(directory)
        server.DASHBOARD_SECRET = ''
        server.ha_client.load_config = lambda: None
        server.settings_store.apply_connection_overrides = lambda: None
        server.settings_store.load_settings = dict
        server._start_ha_polling = lambda: None
        server._start_version_check = lambda: None
        initialized = []
        async def initialize(): initialized.append(True)
        server._select_and_start_data_source = initialize
        async def check():
            async with server.lifespan(server.app):
                assert initialized == [True]
                assert server._push_service.workers == []
                async with AsyncClient(transport=ASGITransport(app=server.app), base_url='https://dashboard.test') as client:
                    status = (await client.get('/api/notifications/status')).json()
                    assert status['enabled'] is True and status['available'] is False
                    assert status['reason'] == 'unsupported_platform' and status['publicKey'] is None
                    assert (await client.get('/health/live')).status_code == 200
                    assert (await client.get('/')).status_code == 200
                    response = await client.post('/api/notifications/test', json={'endpoint':'https://fcm.googleapis.com/send/test'}, headers={'Origin':'https://dashboard.test'})
                    assert response.status_code == 503
            assert not directory.exists()
            assert denied == [], denied
            print(json.dumps({'status':'passed','denied_imports':denied}))
        asyncio.run(check())
        """
    )
    environment = dict(os.environ, PYTHONPATH=str(Path("src").resolve()))
    result = subprocess.run(
        [sys.executable, "-c", script, system],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    assert json.loads(result.stdout) == {"status": "passed", "denied_imports": []}


@pytest.mark.parametrize(("system", "machine"), [("Linux", "x86_64"), ("Darwin", "arm64")])
async def test_supported_sender_still_initializes_real_crypto_and_private_store(
    tmp_path, monkeypatch, system, machine
):
    monkeypatch.setattr(push_service.platform, "system", lambda: system)
    monkeypatch.setattr(push_service.platform, "machine", lambda: machine)
    service = push_service.PushService(
        tmp_path, "https://github.com/victron-venus/inverter-dashboard"
    )
    try:
        status = service.status()
        assert status["available"] and status["reason"] is None
        assert status["publicKey"] and (tmp_path / "push.sqlite3").is_file()
    finally:
        await service.close()
