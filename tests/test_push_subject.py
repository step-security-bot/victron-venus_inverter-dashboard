"""Contact URI validation is identical at configuration and signing boundaries."""

import pytest

from inverter_dashboard.config import Config
from inverter_dashboard.push_subject import validate_push_subject
from inverter_dashboard.push_transport import PushTransport


@pytest.mark.parametrize(
    "subject",
    [
        "https://github.com/victron-venus/inverter-dashboard",
        "https://example.com/contact/team?project=dashboard",
        "https://example.com:8443/contact",
        "https://xn--bcher-kva.example/contact",
        "mailto:notifications+dashboard@example.com",
    ],
)
def test_valid_contact_uris_preserve_exact_identity(subject):
    assert validate_push_subject(subject) == subject
    assert Config(WEB_PUSH_ENABLED=False, WEB_PUSH_SUBJECT=subject).WEB_PUSH_SUBJECT == subject


@pytest.mark.parametrize(
    "subject",
    [
        "",
        "http://example.com/contact",
        "https://",
        "https://user@example.com/contact",
        "https://:password@example.com",
        "https://example.com/#fragment",
        "https://example.com/#",
        "https://example.com:bad/contact",
        "https://example.com:65536/contact",
        "https://example.com:/",
        "https://example.com:0/contact",
        "https://example.com/contact\n",
        "https://example.com/%0d%0a",
        "https://example.com/%oops",
        "https://example.com\\@other.test",
        "https://bücher.example/",
        "https://bad..example/contact",
        "https://-bad.example/",
        "mailto:@example.com",
        "mailto:a@",
        "mailto:a@::1",
        "mailto:a@b@example.com",
        "mailto:a@example.com?subject=test",
        "mailto://a@example.com",
    ],
)
def test_invalid_subject_rejected_before_private_key_access(subject):
    class NoStoreAccess:
        """Reject any private-key access during invalid subject construction."""

        def metadata(self, _key):
            raise AssertionError("Invalid contact reached private storage")

    with pytest.raises(ValueError):
        validate_push_subject(subject)
    with pytest.raises(ValueError):
        Config(WEB_PUSH_ENABLED=False, WEB_PUSH_SUBJECT=subject)
    with pytest.raises(ValueError):
        PushTransport(NoStoreAccess(), subject)
