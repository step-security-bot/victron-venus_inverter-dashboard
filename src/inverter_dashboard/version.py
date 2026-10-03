"""
Version management and self-update functionality
"""

import logging
import os
import sys

import httpx

from .config import GITHUB_RAW_URL, SELF_UPDATE_ENABLED, UPDATE_PIN

logger = logging.getLogger(__name__)


class SelfUpdateDisabled(Exception):
    """Raised when self-update is attempted but not enabled."""


def get_version() -> str:
    """Read version from VERSION file — works as script, package, Docker, or frozen binary.

    Docker images install the package non-editable (``uv sync --no-editable``) while
    also copying ``VERSION`` to ``/app/VERSION``. Prefer an env override, then
    package-adjacent / repo-root / Docker paths, then ``dev``.
    """
    env = (os.environ.get("INVERTER_DASHBOARD_VERSION") or "").strip()
    if env:
        return env

    candidates: list[str] = []
    if getattr(sys, "frozen", False):
        candidates.append(os.path.join(sys._MEIPASS, "VERSION"))  # pylint: disable=protected-access
    else:
        here = os.path.dirname(os.path.abspath(__file__))
        candidates.extend(
            [
                os.path.join(here, "VERSION"),  # packaged next to this module
                os.path.normpath(os.path.join(here, "..", "..", "VERSION")),  # src layout
                "/app/VERSION",  # Docker runtime COPY
            ]
        )

    for version_path in candidates:
        try:
            with open(version_path, encoding="utf-8") as f:
                value = f.read().strip()
            if value:
                return value
        except OSError:
            continue
    return "dev"


VERSION = get_version()


def _update_url(filename: str) -> str:
    """Build URL for a given file, respecting UPDATE_PIN."""
    if UPDATE_PIN:
        return (
            f"https://raw.githubusercontent.com/victron-venus/inverter-dashboard/"
            f"{UPDATE_PIN}/{filename}"
        )
    return f"{GITHUB_RAW_URL}/{filename}"


async def check_latest_version() -> str | None:
    """Check GitHub for latest version from VERSION file on main branch"""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(_update_url("VERSION"), timeout=3.0)
            if resp.status_code == 200:
                latest = resp.text.strip()
                logger.info("Latest version: %s, current: %s", latest, VERSION)
                return latest
    except httpx.HTTPError as e:
        logger.warning("Failed to check latest version: %s", e)
    return None


def _repo_root() -> str:
    """App checkout root (works for the src-layout and flat installs)."""
    return os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))


def download_and_update() -> tuple[bool, str]:
    """Fast-forward the app checkout to origin/main (or UPDATE_PIN) via git.

    Mirrors entrypoint.sh's startup update: after the caller exits the
    process, the container supervisor restarts into the new code.
    Self-update disabled by default. Enable with SELF_UPDATE_ENABLED=true.
    """
    if not SELF_UPDATE_ENABLED:
        raise SelfUpdateDisabled("self-update is disabled (set SELF_UPDATE_ENABLED=true)")

    import subprocess

    ref = UPDATE_PIN or "main"
    root = _repo_root()
    try:
        subprocess.run(
            ["git", "fetch", "origin", ref],
            cwd=root,
            timeout=60,
            check=True,
            capture_output=True,
        )
        target = "FETCH_HEAD" if UPDATE_PIN else "origin/main"
        subprocess.run(
            ["git", "reset", "--hard", target],
            cwd=root,
            timeout=30,
            check=True,
            capture_output=True,
        )
    except (subprocess.SubprocessError, OSError) as e:
        logger.warning("Self-update failed: %s", e)
        # This message is returned by /api/update; keep subprocess arguments,
        # filesystem paths and other exception details in the server log only.
        return False, "git update failed"

    new_version = get_version()
    logger.info("Self-updated to %s — restarting", new_version)
    return True, new_version
