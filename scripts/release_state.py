#!/usr/bin/env python3
# Vendored release toolkit; change the toolkit source, then render again.
# ruff: noqa
# mypy: ignore-errors
# pylint: skip-file
# fmt: off
"""Reserve immutable version plans through a repository-scoped GitHub CAS ledger.

The dedicated branch is metadata only. No operation changes the default branch,
an existing plan, a release tag, or published package bytes.
"""

from __future__ import annotations

import base64
import subprocess

import release_control as rc
import version_plan

BRANCH = "release-version-state"
FILE = "release-version-state.json"
READ_PATH = f"contents/{FILE}?ref={BRANCH}"
WRITE_PATH = f"contents/{FILE}"
REF_PATH = f"git/ref/heads/{BRANCH}"


class StateGitHub(rc.GitHub):
    """Extend the release client's narrow API only for the dedicated state file."""

    def request(self, path, method="GET", body=None, mode="json"):
        # Keep this independently bounded transport compatible with ledger callers.
        # pylint: disable=duplicate-code
        is_read = method == "GET" and path in {READ_PATH, REF_PATH} and body is None
        is_write = method == "PUT" and path == WRITE_PATH and isinstance(body, dict)
        if not (is_read or is_write):
            return super().request(path, method, body, mode)
        rc.require(mode == "json", "Ledger API supports only JSON")
        if is_write:
            rc.require(body.get("branch") == BRANCH, "Ledger write branch mismatch")
            rc.require(
                set(body) <= {"branch", "content", "message", "sha"},
                "Invalid ledger write",
            )
        return self.response(
            subprocess.run(
                [
                    "gh",
                    "api",
                    "--hostname",
                    "github.com",
                    "--method",
                    method,
                    *(["--input", "-"] if body else []),
                    "--",
                    f"{self.base}/{path}",
                ],
                input=rc.json_bytes(body) if body else None,
                capture_output=True,
                check=False,
            ),
            f"{method} {self.base}/{path}",
        )


def read_state(gh: StateGitHub) -> tuple[dict, str | None]:
    """Return the complete ledger and its compare-and-swap blob identity."""
    # JSON booleans must not pass as integer schema, counter or floor values.
    # pylint: disable=unidiomatic-typecheck
    content = gh.optional(READ_PATH)
    if content is None:
        rc.require(
            gh.optional(REF_PATH) is None,
            "Version ledger is missing from its existing branch; restore it from history",
        )
        return {"schema": 1, "counter": 0, "publication_floor": 0, "plans": {}}, None
    rc.require(
        content.get("type") == "file" and content.get("encoding") == "base64",
        "Ledger must be a regular JSON file",
    )
    raw = base64.b64decode("".join(content["content"].split()), validate=True)
    rc.require(
        len(raw) <= 4_000_000, "Ledger requires compaction before another allocation"
    )
    state = rc.parse_json(raw, "release version ledger")
    rc.require(
        isinstance(state, dict)
        and type(state.get("schema")) is int
        and state["schema"] == 1,
        "Unsupported version ledger schema",
    )
    rc.require(
        type(state.get("counter")) is int and state["counter"] >= 0,
        "Invalid ledger counter",
    )
    rc.require(
        type(state.get("publication_floor")) is int
        and 0 <= state["publication_floor"] <= state["counter"],
        "Invalid ledger publication floor",
    )
    rc.require(isinstance(state.get("plans"), dict), "Invalid ledger plans")
    for key, record in state["plans"].items():
        rc.positive(key, "ledger run ID")
        rc.require(isinstance(record, dict), "Invalid version reservation")
        plan = version_plan.validate_plan(record.get("plan"))
        rc.require(
            plan["build_number"] <= state["counter"], "Ledger counter moved backwards"
        )
    rc.require(rc.SHA_RE.fullmatch(content.get("sha", "")), "Invalid ledger blob SHA")
    return state, content["sha"]


def write_state(gh: StateGitHub, state: dict, previous: str | None) -> None:
    """GitHub rejects a stale blob SHA; never retry a conflicting write silently."""
    body = {
        "branch": BRANCH,
        "message": "Reserve release version plan",
        "content": base64.b64encode(rc.json_bytes(state)).decode(),
    }
    if previous:
        body["sha"] = previous
    gh.api(WRITE_PATH, "PUT", body)


# Keep every immutable reservation input and its CAS state explicit.
# pylint: disable-next=too-many-arguments,too-many-locals
def reserve_plan(
    gh: StateGitHub,
    policy: dict,
    base: str,
    channel: str,
    source_sha: str,
    run_id: int,
    attempt: int,
    now,
    parent: dict | None = None,
) -> dict:
    """Allocate once per Actions run and bind retries to the exact same inputs."""
    # A boolean policy floor must not pass as an integer.
    # pylint: disable=unidiomatic-typecheck
    run_key = str(rc.positive(run_id, "run ID"))
    state, previous = read_state(gh)
    if run_key in state["plans"]:
        record = state["plans"][run_key]
        plan = version_plan.validate_plan(record["plan"], policy, source_sha)
        rc.require(
            plan["base_version"] == base and plan["channel"] == channel,
            "Run already reserved a different release identity",
        )
        rc.require(
            record.get("parent") == parent, "Run already reserved a different RC"
        )
        return plan
    sequence = None
    if channel in {"beta", "rc"}:
        sequence = rc.next_sequence(gh, base, channel)
        reserved = [
            record["plan"]["sequence"]
            for record in state["plans"].values()
            if record["plan"]["base_version"] == base
            and record["plan"]["channel"] == channel
        ]
        sequence = max(sequence, max(reserved, default=0) + 1)
    elif channel == "nightly":
        sequence = now.strftime("%Y%m%d%H%M%S") + f".{run_id}.{attempt}"
    floor = policy["versioning"].get("build_number_floor", 0)
    rc.require(type(floor) is int and floor >= 0, "Invalid native build counter floor")
    number = max(state["counter"], floor) + 1
    plan = version_plan.create_plan(base, channel, sequence, source_sha, policy, number)
    rc.ensure_absent(gh, plan["tag"])
    if gh.optional(REF_PATH) is None:
        gh.api("git/refs", "POST", {"ref": f"refs/heads/{BRANCH}", "sha": source_sha})
    state["counter"] = number
    state["plans"][run_key] = {"plan": plan, "parent": parent}
    write_state(gh, state, previous)
    return plan


def verify_reservation(
    gh: StateGitHub, plan: dict, run_id: int, parent: dict | None = None
) -> None:
    """Bind publication to its durable plan and prevent delayed numeric downgrades."""
    state, _ = read_state(gh)
    _verify_reservation(gh, state, plan, run_id, parent)


def _verify_reservation(
    gh: StateGitHub, state: dict, plan: dict, run_id: int, parent: dict | None
) -> None:
    version_plan.validate_plan(plan)
    record = state["plans"].get(str(rc.positive(run_id, "run ID")))
    rc.require(
        record == {"plan": plan, "parent": parent},
        "Version reservation differs from build plan",
    )
    rc.require(
        plan["build_number"] > state["publication_floor"],
        "Build number has already reached or fallen below the publication floor",
    )
    published = {
        release.get("tag_name")
        for release in gh.pages("releases")
        if release.get("draft") is False
    }
    newer = [
        item["plan"]
        for item in state["plans"].values()
        if item["plan"]["tag"] in published
        and item["plan"]["build_number"] >= plan["build_number"]
    ]
    rc.require(
        not newer, "A package with this or a newer build number was already published"
    )


def verify_promotion_order(gh: StateGitHub, plan: dict) -> None:
    """Keep an accepted RC's native counter safe when copying it to stable.

    The selected RC is already published and is intentionally ignored. A later
    publication attempt, including another base/version line of this same app,
    makes promotion of the older native package an unsafe version downgrade.
    """
    state, _ = read_state(gh)
    _verify_promotion_order(gh, state, plan)


def _verify_promotion_order(gh: StateGitHub, state: dict, plan: dict) -> None:
    version_plan.validate_plan(plan)
    rc.require(
        plan["channel"] == "rc" and plan["promotion"] == "promote-bytes",
        "Byte promotion requires a promote-bytes RC plan",
    )
    matching = [record for record in state["plans"].values() if record["plan"] == plan]
    rc.require(
        len(matching) == 1, "Accepted RC has no unique durable version reservation"
    )
    rc.require(
        plan["build_number"] >= state["publication_floor"],
        "Accepted RC build number is below the publication floor",
    )
    published = {
        release.get("tag_name")
        for release in gh.pages("releases")
        if release.get("draft") is False
    }
    rc.require(plan["tag"] in published, "Accepted RC is no longer published")
    newer = [
        record["plan"]
        for record in state["plans"].values()
        if record["plan"]["tag"] != plan["tag"]
        and record["plan"]["tag"] in published
        and record["plan"]["build_number"] > plan["build_number"]
    ]
    rc.require(not newer, "A newer native build was published after the accepted RC")


def begin_publication(
    gh: StateGitHub,
    plan: dict,
    run_id: int,
    parent: dict | None = None,
    promotion: bool = False,
) -> None:
    """Consume the package number with CAS before the first public tag mutation.

    Publication failure still consumes the number. A candidate or final build
    needs a new reservation in a new run; the ledger must never be reset to retry.
    Byte promotion intentionally keeps the already-published RC's number.
    """
    rc.positive(run_id, "run ID")
    # Truthy strings are not valid publication flags.
    # pylint: disable-next=unidiomatic-typecheck
    rc.require(type(promotion) is bool, "Invalid publication promotion flag")
    state, previous = read_state(gh)
    if promotion:
        _verify_promotion_order(gh, state, plan)
    else:
        _verify_reservation(gh, state, plan, run_id, parent)
    state["publication_floor"] = plan["build_number"]
    write_state(gh, state, previous)
