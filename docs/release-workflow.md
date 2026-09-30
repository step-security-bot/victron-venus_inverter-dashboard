# CI and release operations — victron-venus/inverter-dashboard

The source of truth is `.release-policy.json`. `quality-gate.yml` runs the callable
validation workflows and produces the required **CI gate** status on every PR
and merge-queue commit. Superseded PR runs are cancelled. Without an explicit
`change_scope` policy, every change keeps full CI and normal release preparation.
Documentation-only skipping requires that explicit opt-in. When enabled, the
Change scope job checks the complete Git diff first; the gate accepts only proven
documentation skips. Missing, failed or unexpectedly skipped workflows fail the
gate. Unknown files, incomplete history, code, workflows and lockfile changes run
full validation.

The optional `change_scope` policy provides exact `documentation_paths`, exact
`required_paths` for documentation used as a build input, and
`always_validate_workflows` for independently required checks. Documentation paths
cannot exempt source, tests, fixtures, build configuration or dependencies.
Manual dispatch, scheduled runs and release qualification remain full. Only with
explicit `change_scope` opt-in does a documentation-only push stop before release
preparation, version allocation, artifact builds or publication. This does not
change the configured nightly policy.

## Local checks

Use Python 3.11+ for the CLI and the project toolchains documented in `scripts/ci.sh`.
The scripts fail on missing dependencies and do not publish anything during checks.

```bash
python3 scripts/release.py check
python3 scripts/release.py status
```

Callable validation workflows:
- `.github/workflows/ci.yml`
- `.github/workflows/codeql.yml`
- `.github/workflows/python-security.yml`
- `.github/workflows/trivy-fs.yml`
- `.github/workflows/dependency-review.yml`

The [release strategy](../RELEASING.md) defines versioning, channels, acceptance,
ownership, hotfixes and rollback. This document is the operational runbook.

## Nightly, beta and RC

During rollout, checks and builds run but public candidate publication is disabled
until repository variable `RELEASE_CHANNELS_ENABLED=true`. Enable it only after
required release reviewers are configured and legacy production webhooks have
been migrated. This prevents the first nightly/beta from reaching an old auto-deploy
handler. Manual beta/RC/stable requests fail with an explicit configuration error
until enabled; build-only nightlies remain available in Actions artifacts.

Nightly runs daily at the repository's staggered UTC schedule. Default-branch
pushes request beta builds through the same validation and build gates. Publication
also requires the opt-in variable and an eligible unreleased base version. GitHub can delay
scheduled runs; schedule timing is not an SLA. A committed base version (`X.Y.Z`)
is required. Version changes go through PR review, including any native companion
version files. The frozen release plan supplies full candidate versions to declared
format adapters before compilation; the manifest binds the plan and build receipts.

From a clean checkout matching GitHub's default-branch HEAD:

```bash
python3 scripts/release.py package --version 1.2.3 --channel rc
python3 scripts/release.py nightly --dry-run
python3 scripts/release.py beta --version 1.2.3
python3 scripts/release.py rc --version 1.2.3
python3 scripts/release.py status
```

Replace the example version with the committed project version. Native multi-OS
packages require the hosted build matrix; local packaging covers only supported
local targets. These commands never stage unrelated changes, push `main`, or create
tags directly. Publication commands dispatch `release-pipeline.yml` on the default
branch; `package` builds locally, `status` reads run history, and `--dry-run` only
displays the request.

If the base version already has a stable release, bump the committed version through
a PR before beta/RC publication. Nightly builds may still use that existing base.

Candidate tags are unique and immutable: `vX.Y.Z-beta.N`, `vX.Y.Z-rc.N`, or
`vX.Y.Z-nightly.<UTC timestamp>.<run>.<attempt>`. Candidates are prereleases and
never update stable/latest. All required platforms must build before publication.
`release-manifest.json` records source SHA, workflow/run attempt and every payload
SHA-256. The manifest is also saved in immutable Actions evidence for 90 days.

Automatic push betas and scheduled nightlies publish the current default HEAD.
An older source is explicitly marked `superseded` only after ancestry and one
newer automatic replacement run at current HEAD are verified. This does not claim
that the replacement passed or published. No tag, release or promotion evidence
is created for that skipped publication, and its reserved number is not reused.
Missing replacement evidence or API failures remain errors. Manual releases keep
their existing checks. A race after the final check can still fail publication;
inspect the error and dispatch a fresh run at current HEAD, never reset the ledger.

## Stable promotion

After testing the RC on the intended target/environment:

```bash
python3 scripts/release.py doctor
python3 scripts/release.py stable --rc v1.2.3-rc.1 --dry-run
python3 scripts/release.py stable --rc v1.2.3-rc.1
```

Approve the pending `release` environment in GitHub Actions. The publisher checks
that reviewers are configured, verifies the RC's successful run/attempt, default
branch ancestry, Release gate, immutable evidence and every payload checksum.
Stable `vX.Y.Z` copies the tested RC bytes without rebuilding. No override or
force-tag option exists. Expired/missing evidence requires a new RC. A partial
upload remains an unpublished draft; inspect it before any manual recovery.

GitHub releases do not deploy production. Existing push/tag/CI deployment hooks
must be migrated or disabled before enabling automatic prereleases. Container
and PyPI publication use verified stable assets as a separate explicit operation.

```bash
# Verify and display the exact registry operations; requires skopeo and registry login.
python3 scripts/publish_verified.py containers --tag v1.2.3
# Import the same OCI bytes, then optionally move latest after all version tags exist.
python3 scripts/publish_verified.py containers --tag v1.2.3 --execute --latest
```

If publication partially fails, inspect the existing immutable version tags before
retrying. The tool refuses to overwrite them. Never rebuild an image for stable.

## Project limits and rollout requirements

- Local checks use uv with the committed dependency lock and ephemeral lint/test tools; Docker integration additionally runs in CI.
- All four platform binaries retain frozen-application smoke tests. OCI archive keeps the committed SPA; frontend updates are separate reviewed source changes.
- Registry/deployment promotion consumes the approved OCI archive without rebuilding.
- Mandatory validation is locked lint, formatting, tests and 68% application-source coverage (test files excluded) plus security/integration jobs. Broad mypy was previously silently skipped; the existing typing baseline has 63 source errors and mixed src/package test imports, so type checking is an explicit follow-up, not a reported passing gate.

For public repositories, merge and verify the workflows before enabling the
additive Terraform **CI gate** ruleset. Where release/deployment workflows use
environments, configure reviewers and default-branch-only policies. The governance
repositories contain `release-standards.tf` and opt-in examples for public
repositories only. Do not extend these requirements to private repositories by
buying a plan or to workflows that have not landed.

Existing review/security rules remain in force. Physical hardware, real
credentials/streams and production access are not implied by unit tests or builds.

The release engine/client are vendored from `victron-venus/venus-os-ci-toolkit`.
They are excluded from consumer-specific formatting/type policy. Application
release workflows run the mandatory Release tooling contracts job; validation-only
projects receive the local client, whose contracts run in the toolkit. Update the toolkit source and rerun
`scripts/install_release.py`; `--check` detects drift.

References: [GitHub schedules](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule),
[protected environments](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/manage-environments),
[artifact provenance](https://docs.github.com/en/rest/actions/artifacts).

## Automatic version preparation

Run `python3 scripts/release.py prepare-version --pr` from the clean default-branch HEAD. The command refreshes tags and opens a PR with synchronized owned version fields. An existing unreleased base is retained; use `--bump minor`, `--bump major`, or `--version X.Y.Z` for explicit intent. See [version plans](VERSIONING.md) for build overlays, the dedicated allocation ledger and recovery.

A local candidate package also needs the saved `.release-plan.json` at its exact source commit. Restore the `version_plan` object from the published `release-manifest.json` into a disposable checkout before `release.py package`; do not invent a tag or native counter locally. Ordinary development builds can use the project's native build command and explicitly local version identity.
