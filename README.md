# Watchdog

Hermes desktop plugin — system + LCM watchdog visibility layer.

## What it is

A statusbar chip (green "all quiet" / amber "degraded" / red "N problems") plus
a `/watchdog` pane in the Hermes desktop app, backed by a small read-only
FastAPI service on the Hermes host. The pane shows live checks, **watched
sources** (RSS feeds + a GitHub repo, with new-item counts), and an **alert
history** (transition log: open on worsening, resolve on recovery).

**Design principle: one source of truth.** The API shells out to the SAME
check scripts the daily cron watchdog uses
(`~/.hermes/scripts/lcm_daily_check.py` + `lcm_health_check.py`), so the pane
and the cron always agree. The cron stays the alerting layer (Discord,
silent-unless-broken); this plugin is the visibility layer (on-demand, in-app).

## Layout

```
watchdog_api.py            FastAPI service: /health, /status, /alerts,
                           /sources, /config, /run-check
watchdog_config.json       thresholds + watched sources (hot-reloaded)
state/                     runtime state, gitignored: alerts.json (transition
                           log), sources.json (watermark cursors)
desktop-plugin/            plugin.js — copy of the installed desktop plugin
docs/mockup.html           approved mockup
```

## Running the API

```bash
cd ~/workspace/watchdog
uv venv .venv && uv pip install --python .venv/bin/python fastapi "uvicorn[standard]"
.venv/bin/python -m uvicorn watchdog_api:app --host 127.0.0.1 --port 8766
```

Bound to the Tailscale IP only — never 0.0.0.0. Read-only by design
(`/run-check` only re-runs checks). CORS is open because the desktop app
renderer fetches cross-origin; the surface is Tailscale-only. Add a token
before ever exposing it wider.

## Checks surfaced

| id | check | source |
|---|---|---|
| lcm | embedding health (coverage, provider, last embed) + db integrity + summary backlog | `lcm_health_check.py` + direct `lcm.db` reads |
| disk | worst usage vs threshold (config: `thresholds.disk_pct`, default 80) | `df` |
| mem | free + loadavg (informational) | `free`, `/proc/loadavg` |
| processes | ollama, gateway running | `pgrep` |
| cron | jobs.json audit: failures + staleness per cadence | `~/.hermes/cron/jobs.json` |

## Alert history

`update_alerts()` runs on every `/status` poll and records *transitions*, not
snapshots: opening an alert when a check worsens, resolving it on recovery,
updating severity/message on escalation. The **first run is a silent baseline**
— pre-existing problems never spam the history. Persisted atomically to
`state/alerts.json`; capped at `alerts.max_kept` (default 50). Active
(unresolved) alerts always sort above resolved.

## Watched sources

Configured in `watchdog_config.json` → `sources[]`. Each source keeps a
watermark cursor in `state/sources.json`; `/sources` reports new-item counts
since the watermark. RSS uses the item guid (falling back to link, then
title); GitHub compares the latest release tag. Fetch failures degrade that
source only — they never flip the overall chip. `kind`: `rss` (url) or
`github` (repo + ref, default `releases/latest`).

## Plugin install

`plugin.js` lives at `~/.hermes/desktop-plugins/watchdog/plugin.js` (folder
name == plugin id). The desktop app hot-reloads it; if it doesn't appear,
run **⌘K → Reload desktop plugins**. If the Hermes VM's Tailscale IP changes,
update `API_BASE` at the top of `plugin.js`.
