# Vendored release toolkit; change the toolkit source, then render again.
# ruff: noqa
# mypy: ignore-errors
# pylint: skip-file
# fmt: off
"""Exercise consumer publication boundaries against actual frozen input receipts."""

# Dynamic script imports and unittest cleanup are intentional in the vendored suite.
# pylint: disable=wrong-import-position,missing-function-docstring,consider-using-with

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import release_container_labels  # noqa: E402
import release_version_adapter  # noqa: E402
import stage_release_assets  # noqa: E402
import version_plan  # noqa: E402
import version_receipt  # noqa: E402


class ConsumerVersioningTests(unittest.TestCase):
    """Require a source-bound overlay before producing publishable platform assets."""

    def setUp(self):
        self.environment = patch.dict(os.environ)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        os.environ.pop("RELEASE_VERSION_PLAN", None)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / "scripts").mkdir()
        for script in ("release_control.py", "version_plan.py", "version_receipt.py"):
            shutil.copy2(SCRIPTS / script, self.root / "scripts" / script)
        self.policy = {
            "repository": "example/consumer",
            "mode": "release",
            "version_file": "VERSION",
            "versioning": {
                "schema": 1,
                "promotion": "promote-bytes",
                "files": [
                    {"path": "VERSION", "format": "text", "value": "package"},
                    {
                        "path": "package.json",
                        "format": "json",
                        "field": "version",
                        "value": "package",
                    },
                ],
                "artifacts": [
                    {
                        "path": "app.tar.gz",
                        "format": "tar-text",
                        "member": "app/VERSION",
                        "value": "package",
                    }
                ],
            },
        }
        (self.root / ".release-policy.json").write_text(
            json.dumps(self.policy), encoding="utf-8"
        )
        (self.root / "VERSION").write_text("1.2.3\n", encoding="utf-8")
        (self.root / "package.json").write_text(
            '{"name":"consumer","version":"1.2.3"}\n', encoding="utf-8"
        )
        self.originals = {
            name: (self.root / name).read_bytes()
            for name in ("VERSION", "package.json")
        }
        self.command("git", "init", "-q")
        self.command("git", "add", ".")
        self.command(
            "git",
            "-c",
            "user.name=Version fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-qm",
            "Committed base inputs",
        )
        self.source = self.command("git", "rev-parse", "HEAD").strip()

    def command(self, *args):
        return subprocess.check_output(
            args, cwd=self.root, text=True, stderr=subprocess.STDOUT
        )

    def freeze(self, channel="beta"):
        for name, contents in self.originals.items():
            (self.root / name).write_bytes(contents)
        plan = version_plan.create_plan(
            "1.2.3",
            channel,
            None if channel == "stable" else 7,
            self.source,
            self.policy,
            build_number=7,
        )
        (self.root / ".release-plan.json").write_text(
            json.dumps(plan), encoding="utf-8"
        )
        self.command(
            sys.executable,
            str(SCRIPTS / "version_plan.py"),
            "sync",
            "--root",
            str(self.root),
            "--plan",
            ".release-plan.json",
        )
        return plan

    def archive(self):
        output = self.root / "dist"
        output.mkdir(exist_ok=True)
        raw = (self.root / "VERSION").read_bytes()
        with tarfile.open(output / "app.tar.gz", "w:gz", encoding="utf-8") as archive:
            entry = tarfile.TarInfo("app/VERSION")
            entry.size = len(raw)
            archive.addfile(entry, io.BytesIO(raw))

    @staticmethod
    def inventory(directory):
        return [
            {
                "name": path.name,
                "size": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
            for path in sorted(directory.iterdir())
        ]

    def test_local_base_check_does_not_allocate_a_release(self):
        self.assertEqual(
            release_version_adapter.checked_version(self.root, "1.2.3", "beta"), "1.2.3"
        )
        self.assertFalse((self.root / ".release-plan.json").exists())

    def test_staging_requires_plan_before_creating_output(self):
        self.archive()
        with self.assertRaisesRegex(ValueError, "frozen release plan"):
            stage_release_assets.stage(self.root, "native", ["dist/*.tar.gz"])
        self.assertFalse((self.root / "release-assets").exists())

    def test_real_archive_receipt_detects_payload_changes(self):
        plan = self.freeze()
        self.archive()
        output = stage_release_assets.stage(self.root, "native", ["dist/*.tar.gz"])
        version_receipt.verify_receipts(
            output, plan, self.inventory(output), self.policy
        )
        metadata = version_receipt.verify_declared_artifacts(output, self.policy, plan)
        self.assertEqual(len(metadata), 1)
        payload = output / "app.tar.gz"
        payload.write_bytes(payload.read_bytes() + b"changed after build")
        with self.assertRaises(ValueError):
            version_receipt.verify_receipts(
                output, plan, self.inventory(output), self.policy
            )

    def test_staging_rejects_dropped_input_evidence(self):
        self.freeze()
        self.archive()
        path = self.root / ".release-inputs.json"
        evidence = json.loads(path.read_text(encoding="utf-8"))
        evidence["files"].pop()
        path.write_text(json.dumps(evidence), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "every declared version source"):
            stage_release_assets.stage(self.root, "native", ["dist/*.tar.gz"])

    def test_staging_rejects_source_changes_after_overlay(self):
        self.freeze()
        (self.root / "VERSION").write_text("9.9.9\n", encoding="utf-8")
        self.archive()
        with self.assertRaises(ValueError):
            stage_release_assets.stage(self.root, "native", ["dist/*.tar.gz"])

    def test_stable_final_build_requires_explicit_policy_and_frozen_plan(self):
        with self.assertRaisesRegex(ValueError, "promote verified RC"):
            release_version_adapter.checked_version(self.root, "1.2.3", "stable")
        self.policy["versioning"]["promotion"] = "final-build"
        (self.root / ".release-policy.json").write_text(
            json.dumps(self.policy), encoding="utf-8"
        )
        with self.assertRaisesRegex(ValueError, "frozen release plan"):
            release_version_adapter.checked_version(self.root, "1.2.3", "stable")
        self.freeze("stable")
        self.assertEqual(
            release_version_adapter.checked_version(self.root, "1.2.3", "stable"),
            "1.2.3",
        )

    def test_container_labels_follow_package_projection_and_source(self):
        for channel, expected in (("beta", "1.2.3-beta.7"), ("rc", "1.2.3")):
            with self.subTest(channel=channel):
                self.freeze(channel)
                self.assertEqual(
                    release_container_labels.labels(self.root),
                    {
                        "org.opencontainers.image.version": expected,
                        "org.opencontainers.image.revision": self.source,
                    },
                )


if __name__ == "__main__":
    unittest.main()
