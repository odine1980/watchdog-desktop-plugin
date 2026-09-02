# Contributing

This project is small on purpose: a single-file ESM desktop plugin plus a
FastAPI status service. Keep it that way.

## Release workflow (version stamping)

The plugin version lives in exactly one place: the **git tag**.
`scripts/stamp-version.sh` reads the newest tag and stamps it into both the
`@version` header line and the `version:` field of `plugin.js` — the version
never needs hand-editing.

```bash
git tag v1.0.3
./scripts/stamp-version.sh     # stamps "1.0.3" into desktop-plugin/plugin.js
git add desktop-plugin/plugin.js CHANGELOG.md
git commit -m "release v1.0.3"
git push --follow-tags
```

Notes:

- `plugin.js` must keep the ` * @version X.Y.Z` header line or the script
  refuses to run.
- Bump the version only on behavior changes. Docs-only commits do not need a
  new tag.

## Sync policy (maintainers)

The repo copy of the plugin keeps `WATCHDOG_BACKEND_URL` at the public default
(`http://127.0.0.1:8766`). Deployed copies may point at a LAN/Tailscale
address so the desktop app's renderer can reach the backend — **private IPs
never go in this repo.** When syncing to a deployed install, copy then rewrite
the URL; never commit the deployed value.
