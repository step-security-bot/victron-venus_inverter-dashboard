"""Rebuild the reviewed, source-identical http-ece wheel without network access.

Supply a wheelhouse containing the hash-pinned build tools listed in
vendor/http-ece/build-requirements.txt. The upstream sdist and license are in
this repository. Runtime crypto source is verified byte-for-byte after build.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import venv
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "vendor" / "http-ece"
SDIST_SHA = "8c6ab23116bbf6affda894acfd5f2ca0fb8facbcbb72121c11c75c33e7ce8cff"
SOURCE_DATE_EPOCH = "1723075847"


def verify_runtime(sdist: Path, wheel: Path) -> dict[str, str]:
    with tarfile.open(sdist) as archive, zipfile.ZipFile(wheel) as built:
        expected = {}
        for member in archive.getmembers():
            name = member.name.removeprefix("http_ece-1.2.1/")
            if name.startswith("http_ece/") and name.endswith(".py") and "/tests/" not in name:
                original = archive.extractfile(member).read()
                if built.read(name) != original:
                    raise ValueError("Wheel runtime differs from upstream source")
                expected[name] = hashlib.sha256(original).hexdigest()
        if set(expected) != {name for name in built.namelist() if name.endswith(".py")}:
            raise ValueError("Unexpected wheel runtime contents")
        return expected


def output_directory(name: str) -> Path:
    """Keep all writes below the fixed repository build directory."""
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name) is None:
        raise ValueError("Output name must be a simple directory name")
    base = ROOT / "build" / "vendor-wheels"
    output = base / name
    if any(path.is_symlink() for path in (output, *output.parents)):
        raise ValueError("Output must not traverse symlinks")
    if not output.resolve().is_relative_to(base.resolve()):
        raise ValueError("Output must remain inside the build directory")
    return output


def rebuild(wheelhouse: Path, output_name: str) -> Path:
    output = output_directory(output_name)
    if sys.version_info[:2] != (3, 12):
        raise ValueError("Use the reviewed Python 3.12 build interpreter")
    sdist = VENDOR / "http_ece-1.2.1.tar.gz"
    if hashlib.sha256(sdist.read_bytes()).hexdigest() != SDIST_SHA:
        raise ValueError("Upstream source hash mismatch")
    for item in json.loads((VENDOR / "upstream-inputs.json").read_text())[1:]:
        if (
            hashlib.sha256((wheelhouse / item["filename"]).read_bytes()).hexdigest()
            != item["sha256"]
        ):
            raise ValueError("Build wheel hash mismatch")
    output.mkdir(parents=True, exist_ok=True)
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PIP_", "PYTHON", "SETUPTOOLS_"))
    }
    environment.update(
        {
            "SOURCE_DATE_EPOCH": SOURCE_DATE_EPOCH,
            "PYTHONHASHSEED": "0",
            "PIP_NO_INDEX": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        }
    )
    with tempfile.TemporaryDirectory(prefix="http-ece-build-") as temporary:
        work = Path(temporary)
        venv.EnvBuilder(with_pip=False).create(work / "venv")
        python = work / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        bootstrap = next(
            item
            for item in json.loads((VENDOR / "upstream-inputs.json").read_text())
            if item["name"] == "pip"
        )
        install_environment = {
            **environment,
            "PYTHONPATH": str((wheelhouse / bootstrap["filename"]).resolve()),
        }
        subprocess.run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--no-index",
                "--only-binary=:all:",
                "--find-links",
                str(wheelhouse.resolve()),
                "--require-hashes",
                "-r",
                str(VENDOR / "build-requirements.txt"),
            ],
            check=True,
            env=install_environment,
        )
        with tarfile.open(sdist) as archive:
            archive.extractall(work, filter="data")
        source = work / "http_ece-1.2.1"
        # The MIT text is omitted from the upstream sdist, so bundle its exact
        # upstream repository license as packaging metadata, never edited code.
        shutil.copyfile(VENDOR / "LICENSE", source / "LICENSE")
        subprocess.run(
            [str(python), "setup.py", "bdist_wheel", "--dist-dir", str(output.resolve())],
            cwd=source,
            check=True,
            env=environment,
        )
    wheel = output / "http_ece-1.2.1-py2.py3-none-any.whl"
    verify_runtime(sdist, wheel)
    print(hashlib.sha256(wheel.read_bytes()).hexdigest())
    return wheel


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheelhouse", required=True, type=Path)
    parser.add_argument("--output-name", required=True)
    args = parser.parse_args()
    rebuild(args.wheelhouse, args.output_name)


if __name__ == "__main__":
    main()
