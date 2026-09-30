#!/usr/bin/env python3
# Vendored release toolkit; change the toolkit source, then render again.
# ruff: noqa
# mypy: ignore-errors
# pylint: skip-file
# fmt: off
"""Pure release identities and explicitly owned version inputs.

This module never allocates counters, fetches tags, commits, or publishes. A caller
freezes those decisions before building. All file edits are prepared and checked
before the first write, preserve unrelated fields, and reject symlink traversal.
"""

# Kept as one vendored stdlib module. Exact JSON types deliberately reject bool counters.
# pylint: disable=too-many-lines,unidiomatic-typecheck

from __future__ import annotations

import argparse
import ast
import email.parser
import hashlib
import json
import os
import plistlib
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

import tomllib
from release_control import atomic_write_bytes

BASE = re.compile(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\Z", re.ASCII)
SHA = re.compile(r"[0-9a-f]{40}\Z", re.ASCII)
NIGHTLY = re.compile(r"[0-9]{14}\.[1-9][0-9]*\.[1-9][0-9]*\Z", re.ASCII)
FORMATS = {
    "json",
    "toml",
    "cargo-lock",
    "uv-lock",
    "npm-lock",
    "text",
    "python",
    "plist",
    "pbxproj",
}
VALUES = {"base", "full", "package", "build", "apple-build"}
ARTIFACT_FORMATS = {
    "oci",
    "json",
    "wheel",
    "sdist",
    "npm-tar",
    "ipa",
    "plist",
    "tar-text",
    "tar-toml",
    "tar-json",
    "zip-json",
}
MAX_METADATA = 2_000_000


class VersionError(ValueError):
    """A release input does not satisfy the declared contract."""


def require(condition, message):
    """Raise a normal exception (also with Python assertions disabled)."""
    if not condition:
        raise VersionError(message)


def json_bytes(value):
    """Canonical UTF-8 representation used for every identity digest."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode()


def digest(value):
    """Return a SHA-256 digest of exact bytes."""
    return hashlib.sha256(value).hexdigest()


def policy_digest(policy):
    """Bind every policy field to the frozen identity."""
    return digest(json_bytes(policy))


def _field(value):
    if isinstance(value, str):
        result = tuple(value.split("."))
        require(
            all(result),
            "Dotted fields cannot contain empty segments; use a string array",
        )
    else:
        require(
            isinstance(value, list)
            and value
            and all(isinstance(item, str) for item in value),
            "Field must be a dotted string or a nonempty string array",
        )
        result = tuple(value)
    return result


def _relative(value):
    require(
        isinstance(value, str) and value and "\\" not in value and "\0" not in value,
        "Path must be a relative POSIX path",
    )
    path = PurePosixPath(value)
    require(
        not path.is_absolute()
        and all(part not in {"", ".", "..", ".git"} for part in value.split("/")),
        f"Unsafe version path: {value}",
    )
    return path


def _declaration(item, artifact=False):  # pylint: disable=too-many-branches
    require(isinstance(item, dict), "Version declarations must be objects")
    allowed = {"path", "format", "field", "package", "value", "prefix", "ecosystem"}
    if artifact:
        allowed.add("member")
    require(
        not (set(item) - allowed),
        f"Unsupported version declaration fields: {sorted(set(item) - allowed)}",
    )
    require(
        item.get("format") in (ARTIFACT_FORMATS if artifact else FORMATS),
        f"Unsupported version format: {item.get('format')}",
    )
    if "path" in item:
        _relative(item["path"])
    elif not artifact:
        raise VersionError("Version input requires path")
    require(item.get("value", "package") in VALUES, "Unsupported version projection")
    require(
        item.get("ecosystem", "semver") in {"semver", "pep440"},
        "Unsupported package version ecosystem",
    )
    require(
        isinstance(item.get("prefix", ""), str)
        and not any(c in item.get("prefix", "") for c in "\r\n\0"),
        "Invalid version prefix",
    )
    if "field" in item:
        _field(item["field"])
    if "package" in item:
        require(
            isinstance(item["package"], str)
            and bool(item["package"])
            and not any(c in item["package"] for c in "\r\n\0"),
            "Invalid owned package selector",
        )
    kind = item["format"]
    if artifact:
        if kind == "oci":
            require(
                "field" in item and "package" not in item,
                "OCI artifact requires an exact config field and no package selector",
            )
        if kind in {"tar-text", "tar-toml", "tar-json", "zip-json"}:
            require(
                "member" in item, "Archive field verification requires an exact member"
            )
            _relative(item["member"])
        else:
            require("member" not in item, f"{kind} does not accept member")
        if kind == "tar-text":
            require(
                "field" not in item and "package" not in item,
                "Text artifact has no field or package selector",
            )
    if not artifact:
        if kind in {"json", "toml", "python", "plist", "pbxproj"}:
            require("field" in item, f"{kind} requires an explicit field")
        if kind in {"cargo-lock", "uv-lock", "npm-lock", "pbxproj"}:
            require(
                "package" in item, f"{kind} requires an explicit owned package selector"
            )
        if kind in {"text", "cargo-lock", "uv-lock", "npm-lock"}:
            require("field" not in item, f"{kind} does not accept field")
        if kind in {"text", "python", "plist"}:
            require("package" not in item, f"{kind} does not accept package")
    return item


def validate_policy(policy):
    """Validate only this extension; the release engine owns the outer policy."""
    require(isinstance(policy, dict), "Policy must be an object")
    config = policy.get("versioning")
    require(isinstance(config, dict), "Policy requires versioning")
    allowed = {
        "schema",
        "promotion",
        "files",
        "artifacts",
        "build_number_floor",
        "toolkit_revision",
    }
    require(
        not (set(config) - allowed),
        f"Unsupported versioning fields: {sorted(set(config) - allowed)}",
    )
    require(
        type(config.get("schema")) is int and config["schema"] == 1,
        "Unsupported versioning schema",
    )
    require(
        config.get("promotion") in {"promote-bytes", "final-build"},
        "Unsupported promotion profile",
    )
    if "build_number_floor" in config:
        require(
            type(config["build_number_floor"]) is int
            and 0 <= config["build_number_floor"] < 2_100_000_000,
            "Native build number floor must be an integer in 0..2099999999",
        )
    require(
        isinstance(config.get("files"), list) and config["files"],
        "Policy requires explicit version files",
    )
    selectors = set()
    for item in config["files"]:
        _declaration(item)
        selector = (
            item["path"],
            item["format"],
            tuple(_field(item["field"])) if "field" in item else (),
            item.get("package"),
        )
        require(
            selector not in selectors, f"Duplicate version selector: {item['path']}"
        )
        selectors.add(selector)
    artifacts = config.get("artifacts", [])
    require(isinstance(artifacts, list), "Artifact declarations must be an array")
    for item in artifacts:
        _declaration(item, artifact=True)
    if "toolkit_revision" in config:
        require(
            isinstance(config["toolkit_revision"], str)
            and SHA.fullmatch(config["toolkit_revision"]),
            "Toolkit revision must be a pinned SHA",
        )
    return config


def _identity(base, channel, sequence):
    require(
        isinstance(base, str) and BASE.fullmatch(base),
        "Base version must be strict X.Y.Z without prefix or suffix",
    )
    require(
        channel in {"nightly", "beta", "rc", "stable"}, "Unsupported release channel"
    )
    if channel == "stable":
        require(sequence is None, "Stable must not have a sequence")
        return base
    if channel == "nightly":
        require(
            isinstance(sequence, str) and NIGHTLY.fullmatch(sequence),
            "Nightly requires frozen UTCtimestamp.run.attempt suffix",
        )
        # Validate the calendar fields, not just their width.
        try:
            datetime.strptime(sequence.split(".", 1)[0], "%Y%m%d%H%M%S").replace(
                tzinfo=timezone.utc
            )
        except ValueError as error:
            raise VersionError("Invalid nightly UTC timestamp") from error
    else:
        require(
            type(sequence) is int and sequence > 0,
            "Beta/RC sequence must be a positive integer",
        )
    return f"{base}-{channel}.{sequence}"


def create_plan(base, channel, sequence, source_sha, policy, build_number=None):  # pylint: disable=too-many-arguments
    """Create a deterministic identity from already allocated release decisions."""
    config = validate_policy(policy)
    version = _identity(base, channel, sequence)
    require(
        isinstance(source_sha, str) and SHA.fullmatch(source_sha),
        "Source SHA must be a full lowercase commit SHA",
    )
    require(
        build_number is None
        or (type(build_number) is int and 0 < build_number <= 2_100_000_000),
        "Build number must be a positive integer no greater than 2100000000",
    )
    require(
        build_number is None or build_number > config.get("build_number_floor", 0),
        "Build number must exceed the declared migration floor",
    )
    plan = {
        "schema_version": 1,
        "base_version": base,
        "version": version,
        "channel": channel,
        "sequence": sequence,
        "tag": "v" + version,
        "source_sha": source_sha,
        "build_number": build_number,
        "policy_sha256": policy_digest(policy),
        "promotion": config["promotion"],
    }
    for item in config["files"]:
        projected_value(plan, item)
    return plan


def validate_plan(plan, policy=None, source_sha=None):
    """Validate an exact immutable plan and optional source/policy binding."""
    require(isinstance(plan, dict), "Release plan must be an object")
    keys = {
        "schema_version",
        "base_version",
        "version",
        "channel",
        "sequence",
        "tag",
        "source_sha",
        "build_number",
        "policy_sha256",
        "promotion",
    }
    require(
        set(plan) == keys,
        f"Unexpected or missing release-plan fields: {sorted(set(plan) ^ keys)}",
    )
    require(
        type(plan["schema_version"]) is int and plan["schema_version"] == 1,
        "Unsupported release-plan schema",
    )
    version = _identity(plan["base_version"], plan["channel"], plan["sequence"])
    require(
        plan["version"] == version and plan["tag"] == "v" + version,
        "Release version/tag/sequence disagree",
    )
    require(
        isinstance(plan["source_sha"], str) and SHA.fullmatch(plan["source_sha"]),
        "Invalid plan source SHA",
    )
    require(
        isinstance(plan["policy_sha256"], str)
        and re.fullmatch(r"[0-9a-f]{64}", plan["policy_sha256"]),
        "Invalid policy digest",
    )
    require(
        plan["promotion"] in {"promote-bytes", "final-build"},
        "Invalid plan promotion profile",
    )
    build = plan["build_number"]
    require(
        build is None or (type(build) is int and 0 < build <= 2_100_000_000),
        "Invalid native build number",
    )
    if policy is not None:
        config = validate_policy(policy)
        require(
            plan["policy_sha256"] == policy_digest(policy),
            "Release plan policy digest mismatch",
        )
        require(
            plan["promotion"] == config["promotion"],
            "Release plan promotion policy mismatch",
        )
        require(
            build is None or build > config.get("build_number_floor", 0),
            "Plan build number does not exceed the declared migration floor",
        )
        for item in config["files"]:
            projected_value(plan, item)
    if source_sha is not None:
        require(plan["source_sha"] == source_sha, "Release plan source SHA mismatch")
    return plan


def plan_digest(plan):
    """Return the canonical digest after validating the complete plan."""
    validate_plan(plan)
    return digest(json_bytes(plan))


def effective_inputs_digest(evidence):
    """Hash only the actual build inputs, independent of pre-sync file state."""
    entries = []
    seen = set()
    for item in evidence:
        _relative(item["path"])
        require(item["path"] not in seen, "Duplicate effective input path")
        require(
            re.fullmatch(r"[0-9a-f]{64}", item["after_sha256"]),
            "Invalid effective input digest",
        )
        seen.add(item["path"])
        entries.append({"path": item["path"], "sha256": item["after_sha256"]})
    return digest(json_bytes(sorted(entries, key=lambda item: item["path"])))


def projections(plan, ecosystem="semver"):
    """Map identity into package and numeric platform representations."""
    require(ecosystem in {"semver", "pep440"}, "Unsupported package ecosystem")
    package = plan["version"]
    if plan["channel"] == "rc" and plan["promotion"] == "promote-bytes":
        package = plan["base_version"]
    elif ecosystem == "pep440":
        base, channel, sequence = (
            plan["base_version"],
            plan["channel"],
            plan["sequence"],
        )
        if channel == "beta":
            package = f"{base}b{sequence}"
        elif channel == "rc":
            package = f"{base}rc{sequence}"
        elif channel == "nightly":
            # uv stores each PEP 440 numeric component in a bounded u64.
            # GitHub run IDs provide ordering/uniqueness; reserve six digits
            # for attempts. Keep UTC time in the full identity and frozen plan.
            _, run, attempt = sequence.split(".")
            number = int(run) * 1_000_000 + int(attempt)
            require(
                int(attempt) < 1_000_000 and number <= 2**64 - 2,
                "Nightly run or attempt is too large for PEP 440 projection",
            )
            package = f"{base}.dev{number}"
    build = plan["build_number"]
    apple = None
    if build is not None and 0 < build <= 99_990_000:
        n = build - 1
        apple = f"{n // 10000 + 1}.{n // 100 % 100}.{n % 100}"
    return {
        "base": plan["base_version"],
        "full": plan["version"],
        "package": package,
        "build": build,
        "apple-build": apple,
    }


def projected_value(plan, declaration):
    """Compute the one explicitly declared field projection."""
    kind = declaration.get("value", "package")
    result = projections(plan, declaration.get("ecosystem", "semver"))[kind]
    require(
        result is not None,
        f"Projection {kind} requires a valid allocated native build number",
    )
    prefix = declaration.get("prefix", "")
    return prefix + str(result) if prefix else result


def _path(root, name):
    relative = _relative(name)
    root = Path(root).resolve(strict=True)
    path = root
    for component in relative.parts:
        path = path / component
        require(not path.is_symlink(), f"Symlink version input is forbidden: {name}")
    require(
        path.is_file() and path.resolve().is_relative_to(root),
        f"Version input is not a confined regular file: {name}",
    )
    require(
        path.stat().st_nlink == 1, f"Hard-linked version input is forbidden: {name}"
    )
    require(path.stat().st_size <= MAX_METADATA, f"Version input is too large: {name}")
    return path


def _get(data, path):
    current = data
    for key in path:
        require(isinstance(current, (dict, list)), f"Missing version field: {path}")
        try:
            current = current[key]
        except (KeyError, IndexError, TypeError) as error:
            raise VersionError(f"Missing version field: {path}") from error
    return current


def _json_document(raw):
    """Parse JSON while retaining scalar token locations for minimal edits."""
    text = raw.decode("utf-8")
    spans = {}
    decoder = json.JSONDecoder()

    def whitespace(index):
        while index < len(text) and text[index] in " \n\r\t":
            index += 1
        return index

    def parse(index, path):
        index = whitespace(index)
        start = index
        require(index < len(text), "Truncated JSON input")
        if text[index] == "{":
            result = {}
            index = whitespace(index + 1)
            if index < len(text) and text[index] == "}":
                return result, index + 1
            while True:
                key, end = decoder.raw_decode(text, index)
                require(
                    isinstance(key, str) and key not in result,
                    "Duplicate or invalid JSON key",
                )
                index = whitespace(end)
                require(
                    index < len(text) and text[index] == ":", "Malformed JSON object"
                )
                value, index = parse(index + 1, path + (key,))
                result[key] = value
                index = whitespace(index)
                require(index < len(text), "Truncated JSON object")
                if text[index] == "}":
                    return result, index + 1
                require(text[index] == ",", "Malformed JSON object delimiter")
                index = whitespace(index + 1)
        if text[index] == "[":
            result = []
            index = whitespace(index + 1)
            if index < len(text) and text[index] == "]":
                return result, index + 1
            while True:
                value, index = parse(index, path + (len(result),))
                result.append(value)
                index = whitespace(index)
                require(index < len(text), "Truncated JSON array")
                if text[index] == "]":
                    return result, index + 1
                require(text[index] == ",", "Malformed JSON array delimiter")
                index = whitespace(index + 1)
        value, end = decoder.raw_decode(text, index)
        spans[path] = (start, end)
        return value, end

    data, end = parse(0, ())
    require(whitespace(end) == len(text), "Unexpected content after JSON document")
    return text, data, spans


def _scalar(value, old):
    require(
        isinstance(old, str) or type(old) is int,
        "Owned version field must be a string or integer",
    )
    if type(old) is int:
        require(
            type(value) is int,
            "A numeric version field requires a numeric build projection",
        )
        return value
    return str(value)


def _json_edit(raw, declaration, value):
    text, data, spans = _json_document(raw)
    if declaration["format"] == "npm-lock":
        require(
            isinstance(data, dict) and data.get("name") == declaration["package"],
            "npm lock does not own the selected package",
        )
        fields = [("version",)]
        if "packages" in data:
            own = _get(data, ("packages", ""))
            require(
                isinstance(own, dict)
                and own.get("name", data["name"]) == declaration["package"],
                "npm lock root package identity mismatch",
            )
            fields.append(("packages", "", "version"))
    else:
        fields = [_field(declaration["field"])]
        if "package" in declaration:
            require(
                isinstance(data, dict) and data.get("name") == declaration["package"],
                "JSON manifest does not own the selected package",
            )
    edits = []
    previous = []
    for field in fields:
        old = _get(data, field)
        require(field in spans, "JSON version field is not a scalar")
        start, end = spans[field]
        replacement = _scalar(value, old)
        if old != replacement:
            edits.append((start, end, json.dumps(replacement, ensure_ascii=False)))
        previous.append(old)
    for start, end, replacement in sorted(edits, reverse=True):
        text = text[:start] + replacement + text[end:]
    return text.encode("utf-8"), previous


def _toml_key(key):
    try:
        data = tomllib.loads(key + " = 0")
        result = []
        while isinstance(data, dict) and len(data) == 1:
            name, data = next(iter(data.items()))
            result.append(name)
        require(data == 0 and result, "Unsupported TOML key")
        return tuple(result)
    except tomllib.TOMLDecodeError as error:
        raise VersionError(f"Unsupported TOML key: {key}") from error


def _toml_lexical_state(line, quote, depth):
    """Track continued TOML arrays, inline tables and multiline strings."""
    index = 0
    while index < len(line):
        if quote:
            if quote.startswith('"') and line[index] == "\\":
                index += 2
            elif line.startswith(quote, index):
                index += len(quote)
                quote = None
            else:
                index += 1
        elif line[index] == "#":
            break
        elif line.startswith('"""', index) or line.startswith("'''", index):
            quote = line[index : index + 3]
            index += 3
        elif line[index] in "\"'":
            quote = line[index]
            index += 1
        else:
            if line[index] in "[{":
                depth += 1
            elif line[index] in "]}":
                depth -= 1
            index += 1
    return quote, depth


def _toml_edit(raw, declaration, value):  # pylint: disable=too-many-locals,too-many-branches,too-many-statements
    text = raw.decode("utf-8")
    data = tomllib.loads(text)
    kind = declaration["format"]
    if kind in {"cargo-lock", "uv-lock"}:
        entries = data.get("package", [])
        require(isinstance(entries, list), "Lock file requires package array")
        matches = []
        for index, entry in enumerate(entries):
            if entry.get("name") != declaration["package"]:
                continue
            if kind == "cargo-lock":
                local = "source" not in entry
            else:
                source = entry.get("source", {})
                local = (
                    isinstance(source, dict)
                    and len(source) == 1
                    and next(iter(source), "") in {"editable", "virtual", "directory"}
                )
            if local:
                matches.append(index)
        require(
            len(matches) == 1,
            "Lock selector must identify exactly one owned local package",
        )
        target = ("package", matches[0], "version")
    else:
        target = _field(declaration["field"])
        if "package" in declaration:
            require(
                _get(data, target[:-1] + ("name",)) == declaration["package"],
                "TOML manifest package identity mismatch",
            )
    old = _get(data, target)
    replacement = _scalar(value, old)
    require(
        isinstance(replacement, str) or type(replacement) is int,
        "Unsupported TOML value",
    )
    current = ()
    arrays = {}
    offset = 0
    found = []
    quote, depth = None, 0
    for line in text.splitlines(keepends=True):
        continuation = bool(quote) or depth > 0
        quote, depth = _toml_lexical_state(line, quote, depth)
        if continuation:
            offset += len(line)
            continue
        header = re.fullmatch(r"\s*(\[\[?)(.+?)(\]\]?)\s*(?:#.*)?(?:\r?\n)?", line)
        if header:
            opening, key, closing = header.groups()
            require(len(opening) == len(closing), "Malformed TOML table header")
            current = _toml_key(key)
            if opening == "[[":
                arrays[current] = arrays.get(current, -1) + 1
                current += (arrays[current],)
        else:
            assignment = re.match(r"\s*([^#=\n]+?)\s*=\s*", line)
            if assignment:
                key = _toml_key(assignment.group(1))
                if current + key == target:
                    start = assignment.end()
                    scalar = re.match(
                        r'"(?:[^"\\\r\n]|\\.)*"|\x27[^\x27\r\n]*\x27|[+-]?\d[\d_]*',
                        line[start:],
                    )
                    require(
                        scalar is not None,
                        "Owned TOML value must be a single-line scalar",
                    )
                    end = start + scalar.end()
                    require(
                        re.fullmatch(r"\s*(?:#.*)?(?:\r?\n)?", line[end:]),
                        "Unsupported TOML expression after version value",
                    )
                    if isinstance(replacement, str):
                        token = (
                            "'" + replacement + "'"
                            if scalar.group().startswith("'") and "'" not in replacement
                            else json.dumps(replacement)
                        )
                    else:
                        token = str(replacement)
                    found.append((offset + start, offset + end, token))
        offset += len(line)
    require(
        len(found) == 1,
        "TOML selector must identify exactly one explicit scalar assignment",
    )
    start, end, token = found[0]
    result = text[:start] + token + text[end:]
    require(
        _get(tomllib.loads(result), target) == replacement,
        "TOML replacement did not update selected field",
    )
    return result.encode(), [old]


def _python_edit(raw, declaration, value):
    name = declaration["field"]
    require(
        isinstance(name, str) and name.isidentifier(),
        "Python selector must be a module-level constant name",
    )
    text = raw.decode("utf-8")
    tree = ast.parse(text)
    matches = []
    for node in tree.body:
        targets = (
            node.targets
            if isinstance(node, ast.Assign)
            else [node.target]
            if isinstance(node, ast.AnnAssign)
            else []
        )
        if any(
            isinstance(target, ast.Name) and target.id == name for target in targets
        ):
            require(
                len(targets) == 1 and isinstance(node.value, ast.Constant),
                "Version constant must be an unshared literal assignment",
            )
            matches.append(node.value)
    require(
        len(matches) == 1,
        "Python selector must identify exactly one module-level constant",
    )
    node = matches[0]
    replacement = _scalar(value, node.value)
    if replacement == node.value:
        return raw, [node.value]
    lines = raw.splitlines(keepends=True)
    start = sum(map(len, lines[: node.lineno - 1])) + node.col_offset
    end = sum(map(len, lines[: node.end_lineno - 1])) + node.end_col_offset
    literal = repr(replacement).encode()
    result = raw[:start] + literal + raw[end:]
    ast.parse(result)
    return result, [node.value]


def _plist_edit(raw, declaration, value):
    field = _field(declaration["field"])
    data = plistlib.loads(raw)
    old = _get(data, field)
    parent = _get(data, field[:-1]) if len(field) > 1 else data
    require(isinstance(parent, dict), "Plist version parent must be a dictionary")
    replacement = _scalar(value, old)
    if field[-1] == "CFBundleShortVersionString":
        require(
            isinstance(replacement, str) and BASE.fullmatch(replacement),
            "Apple marketing version requires numeric X.Y.Z",
        )
    if field[-1] == "CFBundleVersion":
        require(
            isinstance(replacement, str)
            and re.fullmatch(r"[1-9]\d{0,3}(?:\.\d{1,2}){0,2}", replacement),
            "Apple bundle version exceeds numeric component limits",
        )
    if old == replacement:
        return raw, [old]
    parent[field[-1]] = replacement
    fmt = (
        plistlib.PlistFormat.FMT_BINARY
        if raw.startswith(b"bplist")
        else plistlib.PlistFormat.FMT_XML
    )
    return plistlib.dumps(data, fmt=fmt, sort_keys=False), [old]


def _pbx_edit(raw, declaration, value):  # pylint: disable=too-many-locals
    text = raw.decode()
    owner = declaration["package"]
    require(
        re.fullmatch(r"[A-Fa-f0-9]{24}", owner),
        "PBX selector requires an exact configuration UUID",
    )
    field = declaration["field"]
    require(
        field in {"MARKETING_VERSION", "CURRENT_PROJECT_VERSION"},
        "Unsupported PBX numeric version field",
    )
    starts = list(
        re.finditer(
            r"(?m)^\s*" + re.escape(owner) + r"(?:\s*/\*[^\n]*?\*/)?\s*=\s*\{", text
        )
    )
    require(len(starts) == 1, "PBX owner UUID must identify one configuration")
    start = starts[0].end() - 1
    # Recognize quoted strings/comments so braces in them do not change scope.
    token = re.compile(r'"(?:[^"\\]|\\.)*"|/\*.*?\*/|//[^\n]*|[{}]', re.DOTALL)
    depth = 0
    end = None
    for match in token.finditer(text, start):
        if match.group() == "{":
            depth += 1
        elif match.group() == "}":
            depth -= 1
            if depth == 0:
                end = match.end()
                break
    require(end is not None, "Unterminated PBX configuration")
    block = text[start:end]
    require(
        re.search(r"\bisa\s*=\s*XCBuildConfiguration\s*;", block),
        "PBX owner is not a build configuration",
    )
    matches = list(
        re.finditer(r"(?m)^(\s*" + field + r"\s*=\s*)(\"?[0-9.]+\"?)(\s*;)", block)
    )
    require(
        len(matches) == 1,
        "PBX field must have one numeric assignment in selected configuration",
    )
    replacement = str(value)
    pattern = (
        BASE
        if field == "MARKETING_VERSION"
        else re.compile(r"[1-9]\d{0,3}(?:\.\d{1,2}){0,2}\Z")
    )
    require(
        pattern.fullmatch(replacement), "PBX numeric version is outside platform limits"
    )
    match = matches[0]
    old = match.group(2).strip('"')
    if match.group(2).startswith('"'):
        replacement = '"' + replacement + '"'
    left, right = start + match.start(2), start + match.end(2)
    return (text[:left] + replacement + text[right:]).encode(), [old]


def _edit(raw, declaration, value):
    kind = declaration["format"]
    if kind in {"json", "npm-lock"}:
        return _json_edit(raw, declaration, value)
    if kind in {"toml", "cargo-lock", "uv-lock"}:
        return _toml_edit(raw, declaration, value)
    if kind == "python":
        return _python_edit(raw, declaration, value)
    if kind == "plist":
        return _plist_edit(raw, declaration, value)
    if kind == "pbxproj":
        return _pbx_edit(raw, declaration, value)
    if kind == "text":
        text = raw.decode()
        match = re.fullmatch(r"(\s*)(\S+)(\s*)", text)
        require(
            match is not None,
            "Text version must contain exactly one non-whitespace value",
        )
        return (match.group(1) + str(value) + match.group(3)).encode(), [match.group(2)]
    raise VersionError(f"Unsupported version format: {kind}")


def sync_versions(root, policy, plan, check=False):  # pylint: disable=too-many-locals
    """Validate all edits first; return one digest record per declared input file."""
    validate_plan(plan, policy)
    root = Path(root).resolve(strict=True)
    originals, edited, paths, changes = {}, {}, {}, {}
    for declaration in policy["versioning"]["files"]:
        name = declaration["path"]
        path = _path(root, name)
        if name not in originals:
            originals[name] = path.read_bytes()
            edited[name] = originals[name]
            paths[name] = path
            changes[name] = []
        value = projected_value(plan, declaration)
        updated, old = _edit(edited[name], declaration, value)
        edited[name] = updated
        changes[name].append(
            {
                "format": declaration["format"],
                "field": declaration.get("field"),
                "package": declaration.get("package"),
                "projection": declaration.get("value", "package"),
                "value": value,
                "previous": old,
            }
        )
    # Overlapping selectors are rejected unless every declared expectation still
    # holds after all edits; no last-writer-wins version drift is permitted.
    for declaration in policy["versioning"]["files"]:
        checked, _ = _edit(
            edited[declaration["path"]], declaration, projected_value(plan, declaration)
        )
        require(
            checked == edited[declaration["path"]],
            f"Conflicting version selectors in {declaration['path']}",
        )
    evidence = [
        {
            "path": name,
            "before_sha256": digest(originals[name]),
            "after_sha256": digest(edited[name]),
            "changed": originals[name] != edited[name],
            "fields": changes[name],
        }
        for name in sorted(originals)
    ]
    drift = [item["path"] for item in evidence if item["changed"]]
    if check:
        require(
            not drift, "Version inputs do not match release plan: " + ", ".join(drift)
        )
        return evidence
    # Recheck confinement and file content before committing any prepared edit.
    for name, original in originals.items():
        require(
            _path(root, name) == paths[name] and paths[name].read_bytes() == original,
            f"Version input changed during sync: {name}",
        )
    staged = {}
    try:
        for name in drift:
            path = paths[name]
            fd, temporary = tempfile.mkstemp(prefix=".version-sync-", dir=path.parent)
            staged[name] = Path(temporary)
            with os.fdopen(fd, "wb") as handle:
                handle.write(edited[name])
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, stat.S_IMODE(path.stat().st_mode))
        for name in drift:
            require(
                _path(root, name) == paths[name]
                and paths[name].read_bytes() == originals[name],
                f"Version input changed before replace: {name}",
            )
            os.replace(staged.pop(name), paths[name])
    finally:
        for path in staged.values():
            path.unlink(missing_ok=True)
    return evidence


def read_base_version(root, policy):
    """Read the declared source without asking Git or a moving remote tag."""
    config = validate_policy(policy)
    name = policy.get("version_file")
    require(isinstance(name, str), "Committed version checks require version_file")
    candidates = [
        item
        for item in config["files"]
        if item["path"] == name
        and item.get("value", "package") in {"base", "full", "package"}
    ]
    require(
        len(candidates) == 1, "version_file must have one explicit version selector"
    )
    item = candidates[0]
    raw = _path(root, name).read_bytes()
    # The edit adapter performs ownership validation and reports the old value;
    # this temporary in-memory edit is never written.
    _, old = _edit(raw, item, "0.0.0")
    require(
        len(old) >= 1 and all(value == old[0] for value in old),
        "Base source version fields disagree",
    )
    value = old[0]
    require(isinstance(value, str), "Base version source must be a string")
    prefix = item.get("prefix", "")
    require(value.startswith(prefix), "Base version source prefix mismatch")
    value = value[len(prefix) :]
    require(BASE.fullmatch(value), "Committed base source must contain strict X.Y.Z")
    return value


def check_base_versions(root, policy, base=None):
    """Check committed base fields, excluding allocated native build counters."""
    validate_policy(policy)
    base = base or read_base_version(root, policy)
    filtered = json.loads(json.dumps(policy))
    filtered["versioning"]["files"] = [
        item
        for item in policy["versioning"]["files"]
        if item.get("value", "package") not in {"build", "apple-build"}
    ]
    require(
        filtered["versioning"]["files"], "No committed base version fields are declared"
    )
    plan = create_plan(base, "stable", None, "0" * 40, filtered)
    return sync_versions(root, filtered, plan, check=True)


def verify_checkout(root, policy, plan):
    """Bind CLI overlays to a commit and reject other edits in owned input files.

    Pure adapter tests and source exports need no Git checkout. Release orchestration
    separately verifies all other build inputs and GitHub ancestry.
    """
    root = Path(root).resolve(strict=True)
    if not (root / ".git").exists():
        return
    head = subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
    ).strip()
    require(
        head == plan["source_sha"], "Checkout HEAD differs from release plan source SHA"
    )
    grouped = {}
    for item in policy["versioning"]["files"]:
        grouped.setdefault(item["path"], []).append(item)
    base_plan = {
        **plan,
        "channel": "stable",
        "sequence": None,
        "version": plan["base_version"],
        "tag": "v" + plan["base_version"],
    }
    for name, declarations in grouped.items():
        _relative(name)
        original = subprocess.check_output(
            [
                "git",
                "-C",
                str(root),
                "cat-file",
                "--filters",
                f"--path={name}",
                f"{head}:{name}",
            ]
        )
        expected = original
        for item in declarations:
            if item.get("value", "package") not in {"build", "apple-build"}:
                checked, _ = _edit(original, item, projected_value(base_plan, item))
                require(
                    checked == original,
                    f"Committed version input differs from planned base: {name}",
                )
            expected, _ = _edit(expected, item, projected_value(plan, item))
        current = _path(root, name).read_bytes()
        require(
            current in (original, expected),
            f"Undeclared changes in version input: {name}",
        )


def _metadata_message(raw, expected_name, expected_version):
    require(len(raw) <= MAX_METADATA, "Package metadata is too large")
    message = email.parser.BytesParser().parsebytes(raw, headersonly=True)
    names, versions = message.get_all("Name", []), message.get_all("Version", [])
    require(
        len(names) == len(versions) == 1,
        "Package metadata requires one Name and Version",
    )

    def normalize(name):
        return re.sub(r"[-_.]+", "-", name).lower()

    require(
        normalize(names[0]) == normalize(expected_name),
        "Artifact package name mismatch",
    )
    require(
        versions[0] == expected_version,
        f"Artifact version mismatch: expected {expected_version}, got {versions[0]}",
    )
    return {"package": names[0], "version": versions[0]}


def _zip_metadata(path, predicate):
    with zipfile.ZipFile(path) as archive:
        require(len(archive.infolist()) <= 100_000, "Archive has too many members")
        entries = [
            entry
            for entry in archive.infolist()
            if not entry.is_dir() and predicate(entry.filename)
        ]
        require(
            len(entries) == 1 and entries[0].file_size <= MAX_METADATA,
            "Archive must contain exactly one bounded metadata file",
        )
        require(
            not stat.S_ISLNK(entries[0].external_attr >> 16),
            "Archive metadata must not be a symlink",
        )
        with archive.open(entries[0]) as handle:
            content = handle.read(MAX_METADATA + 1)
        require(len(content) <= MAX_METADATA, "Archive metadata exceeds limit")
        return content


def _tar_metadata(path, predicate):
    matches = []
    with tarfile.open(path, mode="r:*") as archive:
        for count, entry in enumerate(archive):
            require(count < 100_000, "Archive has too many members")
            if predicate(entry.name):
                require(
                    entry.isfile() and entry.size <= MAX_METADATA,
                    "Archive metadata must be a bounded regular file",
                )
                with archive.extractfile(entry) as handle:
                    content = handle.read(MAX_METADATA + 1)
                require(len(content) <= MAX_METADATA, "Archive metadata exceeds limit")
                matches.append(content)
                require(len(matches) == 1, "Ambiguous archive metadata")
    require(len(matches) == 1, "Archive metadata is missing")
    return matches[0]


def _archive_name(name):
    """Allow tar's conventional ./ prefix without accepting traversal paths."""
    while name.startswith("./"):
        name = name[2:]
    try:
        return str(_relative(name))
    except ValueError:
        return None


def _oci_metadata(path, field, expected):  # pylint: disable=too-many-locals,too-many-statements
    """Validate the OCI index/manifest/config digest chain and all runnable images.

    Only bounded JSON metadata is read. Layer bodies remain covered by the outer
    archive receipt; their recorded existence and size are checked here.
    """
    index_types = {
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
    }
    manifest_types = {
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    }
    oci_manifest_type = "application/vnd.oci.image.manifest.v1+json"
    config_types = {
        "application/vnd.oci.image.config.v1+json",
        "application/vnd.docker.container.image.v1+json",
    }
    attestation_type = "application/vnd.docker.attestation.manifest.v1+json"
    empty_config_type = "application/vnd.oci.empty.v1+json"
    empty_config_digest = "sha256:" + digest(b"{}")
    images, verified = [], set()
    runnable_manifests, attestation_subjects, legacy_references = {}, [], []
    visited, metadata_bytes = 0, 0
    with tarfile.open(path, mode="r:*") as archive:
        members = {}
        for count, entry in enumerate(archive):
            require(count < 100_000, "OCI archive has too many members")
            name = _archive_name(entry.name)
            if entry.isdir():
                continue
            require(
                name is not None and name not in members,
                "Unsafe or duplicate OCI archive member",
            )
            members[name] = entry

        def read(name):
            nonlocal metadata_bytes
            entry = members.get(name)
            require(
                entry is not None and entry.isfile() and 0 < entry.size <= MAX_METADATA,
                f"Missing or oversized OCI metadata: {name}",
            )
            with archive.extractfile(entry) as handle:
                raw = handle.read(MAX_METADATA + 1)
            require(len(raw) == entry.size, "Truncated OCI metadata")
            metadata_bytes += len(raw)
            require(
                metadata_bytes <= 32_000_000, "OCI metadata exceeds aggregate limit"
            )
            return raw

        def blob(descriptor, metadata=True):
            require(isinstance(descriptor, dict), "OCI descriptor must be an object")
            identity = descriptor.get("digest", "")
            require(
                isinstance(identity, str)
                and re.fullmatch(r"sha256:[0-9a-f]{64}", identity),
                "OCI descriptor requires a sha256 digest",
            )
            size = descriptor.get("size")
            require(type(size) is int and size >= 0, "Invalid OCI descriptor size")
            name = "blobs/sha256/" + identity[7:]
            entry = members.get(name)
            require(
                entry is not None and entry.isfile() and entry.size == size,
                "OCI descriptor blob is missing or has a different size",
            )
            if not metadata:
                return None
            raw = read(name)
            require(digest(raw) == identity[7:], "OCI metadata digest mismatch")
            verified.add(identity)
            data = _json_document(raw)[1]
            require(isinstance(data, dict), "OCI metadata must be an object")
            return data

        def descriptor_key(descriptor):
            return (
                descriptor.get("mediaType"),
                descriptor.get("digest"),
                descriptor.get("size"),
            )

        def walk(descriptor, depth=0):  # pylint: disable=too-many-branches,too-many-locals,too-many-statements
            nonlocal visited
            visited += 1
            require(depth <= 8 and visited <= 1024, "OCI metadata graph exceeds limits")
            data = blob(descriptor)
            media = descriptor.get("mediaType")
            require(
                data.get("schemaVersion") == 2
                and data.get("mediaType", media) == media,
                "OCI manifest schema or media type mismatch",
            )
            platform = descriptor.get("platform") or {}
            require(isinstance(platform, dict), "Invalid OCI platform descriptor")
            annotations = descriptor.get("annotations") or {}
            require(isinstance(annotations, dict), "Invalid OCI annotations")
            reference_type = annotations.get("vnd.docker.reference.type")
            artifact_type = data.get("artifactType")
            if (
                reference_type == "attestation-manifest"
                or artifact_type == attestation_type
            ):
                require(
                    media in manifest_types
                    and platform.get("os") == "unknown"
                    and platform.get("architecture") == "unknown"
                    and reference_type in {None, "attestation-manifest"},
                    "Ambiguous OCI attestation descriptor",
                )
                attest_config = data.get("config")
                reference_digest = annotations.get("vnd.docker.reference.digest")
                if reference_digest is not None:
                    require(
                        isinstance(reference_digest, str)
                        and re.fullmatch(r"sha256:[0-9a-f]{64}", reference_digest),
                        "Invalid OCI attestation reference digest",
                    )
                if artifact_type is not None:
                    require(
                        artifact_type == attestation_type
                        and media == oci_manifest_type
                        and descriptor.get("artifactType") in {None, attestation_type},
                        "Invalid OCI attestation artifact type",
                    )
                    require(
                        isinstance(attest_config, dict)
                        and attest_config.get("mediaType") == empty_config_type
                        and attest_config.get("digest") == empty_config_digest
                        and attest_config.get("size") == 2
                        and (
                            "data" not in attest_config
                            or attest_config.get("data") == "e30="
                        ),
                        "Invalid OCI attestation config",
                    )
                    require(
                        blob(attest_config) == {},
                        "Invalid OCI attestation config contents",
                    )
                    subject = data.get("subject")
                    require(
                        isinstance(subject, dict)
                        and subject.get("mediaType") == oci_manifest_type
                        and "artifactType" not in subject,
                        "Invalid OCI attestation subject",
                    )
                    blob(subject, metadata=False)
                    subject_platform = subject.get("platform")
                    require(
                        subject_platform is None or isinstance(subject_platform, dict),
                        "Invalid OCI attestation subject platform",
                    )
                    require(
                        reference_digest is None
                        or reference_digest == subject.get("digest"),
                        "OCI attestation reference differs from subject",
                    )
                    attestation_subjects.append(
                        (descriptor_key(subject), subject_platform)
                    )
                else:
                    require(
                        descriptor.get("artifactType") is None
                        and isinstance(attest_config, dict)
                        and attest_config.get("mediaType") in config_types,
                        "Invalid OCI attestation config",
                    )
                    attest_details = blob(attest_config)
                    require(
                        attest_details.get("os") == "unknown"
                        and attest_details.get("architecture") == "unknown",
                        "OCI attestation contains a runnable image",
                    )
                    if reference_digest is not None:
                        legacy_references.append(reference_digest)
                attest_layers = data.get("layers")
                require(
                    isinstance(attest_layers, list) and 0 < len(attest_layers) <= 4096,
                    "Invalid OCI attestation layers",
                )
                for layer in attest_layers:
                    require(
                        isinstance(layer, dict)
                        and layer.get("mediaType") == "application/vnd.in-toto+json",
                        "Unsupported OCI attestation layer",
                    )
                    blob(layer, metadata=False)
                return
            if media in index_types:
                manifests = data.get("manifests")
                require(
                    isinstance(manifests, list) and 0 < len(manifests) <= 1024,
                    "OCI index requires a bounded manifest list",
                )
                for child in manifests:
                    walk(child, depth + 1)
                return
            require(media in manifest_types, "Unsupported OCI descriptor media type")
            config = data.get("config")
            require(
                isinstance(config, dict) and config.get("mediaType") in config_types,
                "OCI image requires a supported config descriptor",
            )
            details = blob(config)
            for key in ("os", "architecture"):
                require(
                    isinstance(details.get(key), str)
                    and details[key] not in {"", "unknown"},
                    "OCI runnable image has no platform identity",
                )
                require(
                    key not in platform or platform[key] == details[key],
                    "OCI platform descriptor differs from config",
                )
            actual = _get(details, field)
            require(
                str(actual) == expected,
                f"OCI image version mismatch: expected {expected}, got {actual}",
            )
            layers = data.get("layers")
            require(
                isinstance(layers, list) and len(layers) <= 4096,
                "Invalid OCI layer inventory",
            )
            for layer in layers:
                blob(layer, metadata=False)
            runnable_manifests[descriptor_key(descriptor)] = {
                "os": details["os"],
                "architecture": details["architecture"],
            }
            images.append(
                {
                    "config_sha256": config["digest"][7:],
                    "os": details["os"],
                    "architecture": details["architecture"],
                    "version": str(actual),
                }
            )

        layout = _json_document(read("oci-layout"))[1]
        require(
            isinstance(layout, dict) and layout.get("imageLayoutVersion") == "1.0.0",
            "Unsupported OCI image layout",
        )
        root_raw = read("index.json")
        index = _json_document(root_raw)[1]
        require(
            isinstance(index, dict)
            and index.get("schemaVersion") == 2
            and index.get("mediaType", "application/vnd.oci.image.index.v1+json")
            in index_types,
            "Invalid OCI root index",
        )
        manifests = index.get("manifests")
        require(
            isinstance(manifests, list) and 0 < len(manifests) <= 1024,
            "OCI root index requires a bounded manifest list",
        )
        for descriptor in manifests:
            walk(descriptor)
        for subject, subject_platform in attestation_subjects:
            target = runnable_manifests.get(subject)
            require(
                target is not None, "OCI attestation subject is not a runnable image"
            )
            if subject_platform is not None:
                for key in ("os", "architecture"):
                    require(
                        key not in subject_platform
                        or subject_platform[key] == target[key],
                        "OCI attestation subject platform differs from image",
                    )
        runnable_digests = {item[1] for item in runnable_manifests}
        for reference_digest in legacy_references:
            require(
                reference_digest in runnable_digests,
                "OCI attestation reference is not a runnable image",
            )
        require(bool(images), "OCI archive has no runnable images")
    return json_bytes(
        {"index_sha256": digest(root_raw), "metadata": sorted(verified)}
    ), images


def verify_artifact(path, declaration, plan):  # pylint: disable=too-many-statements
    """Inspect real metadata without extracting archives or executing payloads."""
    validate_plan(plan)
    _declaration(declaration, artifact=True)
    path = Path(path)
    require(path.is_file() and not path.is_symlink(), "Artifact must be a regular file")
    expected = str(projected_value(plan, declaration))
    kind = declaration["format"]
    if kind == "oci":
        require(
            "field" in declaration and "package" not in declaration,
            "OCI artifact requires an exact config field and no package selector",
        )
        raw, images = _oci_metadata(path, _field(declaration["field"]), expected)
        result = {"field": declaration["field"], "version": expected, "images": images}
    elif kind in {"wheel", "sdist"}:
        require("package" in declaration, "Package artifact requires an owned name")
        if kind == "wheel":
            raw = _zip_metadata(
                path,
                lambda name: (
                    len(name.split("/")) == 2 and name.endswith(".dist-info/METADATA")
                ),
            )
        else:

            def predicate(name):
                return len(name.split("/")) == 2 and name.endswith("/PKG-INFO")

            raw = (
                _zip_metadata(path, predicate)
                if zipfile.is_zipfile(path)
                else _tar_metadata(path, predicate)
            )
        result = _metadata_message(raw, declaration["package"], expected)
    elif kind == "npm-tar":
        require("package" in declaration, "npm artifact requires an owned name")
        raw = _tar_metadata(path, lambda name: name == "package/package.json")
        data = json.loads(raw)
        require(
            data.get("name") == declaration["package"]
            and data.get("version") == expected,
            "npm artifact identity mismatch",
        )
        result = {"package": data["name"], "version": data["version"]}
    elif kind in {"tar-text", "tar-toml", "tar-json", "zip-json"}:
        member = declaration["member"]
        read = _zip_metadata if kind.startswith("zip-") else _tar_metadata
        raw = read(path, lambda name: _archive_name(name) == member)
        if kind == "tar-text":
            actual = raw.decode().strip()
            require(
                bool(actual) and not any(char.isspace() for char in actual),
                "Text artifact version must be one value",
            )
        else:
            require("field" in declaration, "Artifact metadata requires an exact field")
            data = (
                tomllib.loads(raw.decode())
                if kind == "tar-toml"
                else _json_document(raw)[1]
            )
            field = _field(declaration["field"])
            actual = _get(data, field)
            if "package" in declaration:
                name_field = field[:-1] + ("name",) if kind == "tar-toml" else ("name",)
                require(
                    _get(data, name_field) == declaration["package"],
                    "Archive metadata package identity mismatch",
                )
        require(
            str(actual) == expected,
            f"Artifact field mismatch: expected {expected}, got {actual}",
        )
        result = {
            "member": member,
            "field": declaration.get("field"),
            "version": str(actual),
        }
    else:
        require("field" in declaration, "Artifact metadata requires an exact field")
        if kind == "ipa":
            raw = _zip_metadata(
                path,
                lambda name: (
                    name.startswith("Payload/")
                    and name.count("/") == 2
                    and name.endswith(".app/Info.plist")
                ),
            )
            data = plistlib.loads(raw)
        else:
            require(
                path.stat().st_size <= MAX_METADATA, "Artifact metadata is too large"
            )
            raw = path.read_bytes()
            data = plistlib.loads(raw) if kind == "plist" else _json_document(raw)[1]
        actual = _get(data, _field(declaration["field"]))
        require(
            str(actual) == expected,
            f"Artifact field mismatch: expected {expected}, got {actual}",
        )
        result = {"field": declaration["field"], "version": str(actual)}
    return {
        "path": str(path),
        "format": kind,
        "plan_sha256": plan_digest(plan),
        "metadata_sha256": digest(raw),
        **result,
    }


def main(argv=None):  # pylint: disable=too-many-locals
    """Apply or check a frozen plan without allocating release identity."""
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "--check-base":
        args[0] = "check-base"
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    sync = commands.add_parser("sync", help="Apply or check frozen version inputs")
    sync.add_argument("--plan", type=Path, required=True)
    sync.add_argument("--check", action="store_true")
    sync.add_argument("--output", type=Path, default=Path(".release-inputs.json"))
    base = commands.add_parser(
        "check-base", help="Check committed base version companions"
    )
    base.add_argument("--version")
    for command in (sync, base):
        command.add_argument("--root", type=Path, default=Path.cwd())
        command.add_argument(
            "--policy", type=Path, default=Path(".release-policy.json")
        )
    options = parser.parse_args(args)
    try:
        root = options.root.resolve(strict=True)
        policy_path = (
            options.policy if options.policy.is_absolute() else root / options.policy
        )
        policy = json.loads(policy_path.read_bytes())
        if options.command == "check-base":
            result = check_base_versions(root, policy, options.version)
        else:
            plan_path = (
                options.plan if options.plan.is_absolute() else root / options.plan
            )
            plan = json.loads(plan_path.read_bytes())
            validate_plan(plan, policy)
            verify_checkout(root, policy, plan)
            result = sync_versions(root, policy, plan, check=options.check)
            if not options.check:
                output = (
                    options.output
                    if options.output.is_absolute()
                    else root / options.output
                )
                require(
                    output.parent.resolve() == root and not output.is_symlink(),
                    "Evidence output must be a plain file in the checkout root",
                )
                require(
                    output.name
                    not in {item["path"] for item in policy["versioning"]["files"]},
                    "Evidence output must not overwrite a version input",
                )
                atomic_write_bytes(
                    root / output.name,
                    json_bytes(
                        {
                            "schema": 1,
                            "plan_sha256": plan_digest(plan),
                            "source_sha": plan["source_sha"],
                            "files": result,
                            "effective_inputs_sha256": effective_inputs_digest(result),
                        }
                    )
                    + b"\n",
                )
        print(json.dumps(result, indent=2))
        return 0
    except (
        ValueError,
        OSError,
        SyntaxError,
        KeyError,
        TypeError,
        subprocess.CalledProcessError,
        zipfile.BadZipFile,
        tarfile.TarError,
    ) as error:
        print(f"version-plan: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
