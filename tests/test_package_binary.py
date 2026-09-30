"""Exercise binary packaging through its actual CLI in an isolated checkout."""

import hashlib
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def checkout(tmp_path):
    """Copy the CLI and frozen path validator into a disposable build tree."""
    root = tmp_path / "checkout"
    (root / "scripts").mkdir(parents=True)
    (root / "dist").mkdir()
    for name in (
        "package_binary.py",
        "version_receipt.py",
        "version_plan.py",
        "release_control.py",
    ):
        shutil.copy2(REPO / "scripts" / name, root / "scripts" / name)
    (root / "VERSION").write_text("1.2.3-beta.4\n")
    (root / "dist/inverter-dashboard").write_bytes(b"fixture executable")
    (root / "dist/inverter-dashboard.exe").write_bytes(b"fixture windows executable")
    return root


def package(root, binary="dist/inverter-dashboard", platform="linux-x86_64"):
    """Run the real CLI so argument parsing and path validation are covered."""
    return subprocess.run(
        [sys.executable, "scripts/package_binary.py", str(binary), platform],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    ("platform", "binary"),
    [
        ("linux-x86_64", "inverter-dashboard"),
        ("macos-arm64", "inverter-dashboard"),
        ("windows-x86_64", "inverter-dashboard.exe"),
        ("Darwin-arm64", "inverter-dashboard"),
    ],
)
def test_package_contains_verified_binary_and_identity(checkout, platform, binary):
    """CI and existing local platform labels retain their executable metadata."""
    for name in (".release-plan.json", ".release-inputs.json"):
        (checkout / name).write_text('{"fixture": true}\n')
    result = package(checkout, f"dist/{binary}", platform)
    assert result.returncode == 0, result.stderr
    archive = checkout / "release" / f"inverter-dashboard-{platform}.zip"
    with zipfile.ZipFile(archive) as bundle:
        metadata = json.loads(bundle.read("build-info.json"))
        assert metadata["version"] == "1.2.3-beta.4"
        assert metadata["platform"] == platform
        assert metadata["binary"] == binary
        assert metadata["binary_sha256"] == hashlib.sha256(bundle.read(binary)).hexdigest()
        assert json.loads(bundle.read(".release-plan.json")) == {"fixture": True}
        assert json.loads(bundle.read(".release-inputs.json")) == {"fixture": True}
    assert archive.with_suffix(".zip.sha256").read_text() == (
        f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  {archive.name}\n"
    )


def test_package_rejects_outside_traversal_and_symlink_inputs(checkout):
    """No arbitrary local file can be read into a release package."""
    outside = checkout.parent / "outside"
    outside.write_bytes(b"unrelated data")
    (checkout / "dist/linked").symlink_to(outside)
    for requested in (outside, "../outside", "dist/../VERSION", "dist/linked", "VERSION"):
        result = package(checkout, requested)
        assert result.returncode != 0
        assert not (checkout / "release").exists()


def test_package_rejects_unsafe_platform_before_creating_output(checkout):
    """A platform label cannot select directories or alternate archive locations."""
    for platform in ("../../../outside", "/tmp/output", "linux/amd64", "..", ""):
        result = package(checkout, platform=platform)
        assert result.returncode != 0
        assert not (checkout / "release").exists()


def test_package_does_not_follow_output_symlink(checkout):
    """An existing release-directory link cannot redirect artifact writes."""
    outside = checkout.parent / "outside"
    outside.mkdir()
    (checkout / "release").symlink_to(outside, target_is_directory=True)
    result = package(checkout)
    assert result.returncode != 0
    assert not list(outside.iterdir())
