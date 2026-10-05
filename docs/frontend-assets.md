# Embedded frontend assets

The Python package serves the reviewed Vue SPA committed under
`src/inverter_dashboard/static`. Rebuild from a clean checkout of the recorded
frontend source; do not edit the minified JavaScript.

The current source is
[`inverter-dashboard-vue` at `e79054130cca49f919c89e6491bb67769882f3ba`](https://github.com/victron-venus/inverter-dashboard-vue/tree/e79054130cca49f919c89e6491bb67769882f3ba).
That source carries the page's `token` into both the WebSocket URL and the
`/api/state` fallback, preserving live telemetry when `DASHBOARD_SECRET` is set.
Notifications use their source event time, refresh relative age while idle and
on foreground, and expose the exact local date and timezone. Missing or invalid
source times remain unknown through replay and reconnect.

Build with `npm ci --ignore-scripts --no-audit --no-fund`, run `npm test`, then
`npm run build:all`. Copy only the resulting `dist/` contents into this package's
`static/`, removing obsolete files from that destination. The frontend export
script also updates other repositories, so use an explicit destination when
updating only this package.

`static/source-info.json` records the exact clean source commit, lockfile hash,
Node/npm versions, build command and hashes/sizes of every copied output file.
Update that record with each new build. The package tests verify its complete
file inventory and hashes, in addition to serving the SPA's referenced assets.
These hashes detect incomplete or stale copies; they are local build provenance,
not a published release qualification or a substitute for release evidence.
