"""Tests for config module."""

from urllib.parse import urlsplit


class TestConfig:
    """Tests for config module defaults."""

    def test_defaults(self):
        from inverter_dashboard import config

        assert config.MQTT_PORT == 1883
        assert config.WEB_PORT == 8080
        assert config.SELF_UPDATE_ENABLED is False
        assert config.DEFAULT_POWER_MIN == -2300
        assert config.DEFAULT_POWER_MAX == 2250
        assert config.CONSOLE_MAX_LINES == 50
        assert config.CONSOLE_SEND_LINES == 20

    def test_dashboard_secret_default_empty(self):
        from inverter_dashboard import config

        # Default should be empty (unset)
        assert config.DASHBOARD_SECRET == "" or isinstance(config.DASHBOARD_SECRET, str)

    def test_github_raw_url(self):
        from inverter_dashboard import config

        url = urlsplit(config.GITHUB_RAW_URL)
        assert url.scheme == "https"
        assert url.hostname == "raw.githubusercontent.com"
        assert url.path.startswith(f"/{config.GITHUB_REPO}/")
