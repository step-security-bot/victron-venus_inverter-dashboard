"""Tests for version module."""

import pytest

from inverter_dashboard.version import SelfUpdateDisabled, download_and_update, get_version


def _deny_app_version_open(_tmp_path):
    real_open = open

    def _open(path, *args, **kwargs):
        if str(path) == "/app/VERSION":
            raise OSError("blocked")
        return real_open(path, *args, **kwargs)

    return _open


class TestGetVersion:
    """Tests for get_version."""

    def test_returns_string(self):
        result = get_version()
        assert isinstance(result, str)

    def test_returns_non_empty(self):
        result = get_version()
        assert len(result) > 0

    def test_reads_package_adjacent_version(self, tmp_path, monkeypatch):
        """Packaged install: VERSION next to version.py wins."""
        ver = tmp_path / "VERSION"
        ver.write_text("9.9.9\n", encoding="utf-8")
        monkeypatch.setattr(
            "inverter_dashboard.version.__file__",
            str(tmp_path / "version.py"),
        )
        monkeypatch.delattr("sys.frozen", raising=False)
        monkeypatch.delenv("INVERTER_DASHBOARD_VERSION", raising=False)
        assert get_version() == "9.9.9"

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("INVERTER_DASHBOARD_VERSION", "7.7.7")
        assert get_version() == "7.7.7"

    def test_falls_back_to_dev(self, tmp_path, monkeypatch):
        """When VERSION file is missing, returns 'dev'."""
        monkeypatch.setattr(
            "inverter_dashboard.version.__file__",
            str(tmp_path / "nested" / "version.py"),
        )
        monkeypatch.delattr("sys.frozen", raising=False)
        monkeypatch.delenv("INVERTER_DASHBOARD_VERSION", raising=False)
        monkeypatch.setattr("builtins.open", _deny_app_version_open(tmp_path))
        assert get_version() == "dev"


class TestSelfUpdateDisabled:
    """Tests for SelfUpdateDisabled behavior."""

    def test_is_exception(self):
        with pytest.raises(SelfUpdateDisabled):
            raise SelfUpdateDisabled("test")


class TestDownloadAndUpdate:
    """Tests for download_and_update."""

    def _enable(self, monkeypatch, pin=""):
        monkeypatch.setattr("inverter_dashboard.version.SELF_UPDATE_ENABLED", True)
        monkeypatch.setattr("inverter_dashboard.version.UPDATE_PIN", pin)

    def test_raises_when_disabled(self, monkeypatch):
        monkeypatch.setattr("inverter_dashboard.version.SELF_UPDATE_ENABLED", False)
        with pytest.raises(SelfUpdateDisabled):
            download_and_update()

    def test_success_tracks_main(self, monkeypatch):
        self._enable(monkeypatch)
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)

        monkeypatch.setattr("subprocess.run", fake_run)
        ok, msg = download_and_update()
        assert ok is True
        assert isinstance(msg, str) and msg
        assert len(calls) == 2
        assert calls[0][:3] == ["git", "fetch", "origin"]
        assert calls[0][-1] == "main"
        assert calls[1] == ["git", "reset", "--hard", "origin/main"]

    def test_pin_uses_fetch_head(self, monkeypatch):
        self._enable(monkeypatch, pin="v9.9.9")
        cmds = []
        monkeypatch.setattr("subprocess.run", lambda cmd, **k: cmds.append(cmd))
        ok, _ = download_and_update()
        assert ok is True
        assert cmds[0][-1] == "v9.9.9"
        assert cmds[1] == ["git", "reset", "--hard", "FETCH_HEAD"]

    def test_git_failure_returns_false(self, monkeypatch):
        import subprocess

        self._enable(monkeypatch)

        def boom(cmd, **kwargs):
            raise subprocess.CalledProcessError(128, cmd)

        monkeypatch.setattr("subprocess.run", boom)
        ok, msg = download_and_update()
        assert ok is False
        assert msg == "git update failed"

    def test_os_error_returns_false(self, monkeypatch):
        self._enable(monkeypatch)
        monkeypatch.setattr(
            "subprocess.run",
            lambda cmd, **k: (_ for _ in ()).throw(OSError("no repo")),
        )
        ok, msg = download_and_update()
        assert ok is False
        assert msg == "git update failed"
        assert "no repo" not in msg

    @pytest.mark.parametrize("operation", [1, 2])
    def test_git_failure_does_not_return_subprocess_details(self, monkeypatch, caplog, operation):
        import subprocess

        self._enable(monkeypatch)
        calls = 0

        def fail(cmd, **kwargs):
            nonlocal calls
            calls += 1
            if calls == operation:
                raise subprocess.CalledProcessError(128, ["git", "/private/repository"])

        monkeypatch.setattr("subprocess.run", fail)
        assert download_and_update() == (False, "git update failed")
        assert "/private/repository" in caplog.text
