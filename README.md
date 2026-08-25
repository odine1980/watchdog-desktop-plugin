# Watchdog

A statusbar chip + `/watchdog` **plugin** pane for the [Hermes Agent](https://hermes-agent.nousresearch.com) desktop app — a visibility layer for your Hermes host's health, backed by a small read-only FastAPI status service (the plugin's own backend — not the Hermes gateway, not the Hermes web dashboard, not the API server).

## What it is

| Piece | What it does |
|---|---|
| **Statusbar chip** | Green "all quiet" / amber "degraded" / red "N problems" — one glance, no noise |
| **`/watchdog` plugin pane** | Live checks, **watched sources** (RSS feeds + a GitHub repo with new-item counts), and an **alert history** (transition log: opens on worsening, resolves on recovery) |
| **FastAPI backend** | The plugin's own read-only status service — NOT the Hermes gateway (`hermes gateway`), NOT the Hermes web dashboard (`hermes dashboard`, 9119), and NOT the OpenAI-compatible API server (8642). Endpoints: `/health`, `/status`, `/alerts`, `/sources`, `/config`, `/run-check` |

**Design principle: one source of truth.** The backend shells out to the SAME check scripts the daily cron watchdog uses (`~/.hermes/scripts/lcm_daily_check.py` + `lcm_health_check.py`), so the pane and the cron always agree. The cron stays the alerting layer (silent-unless-broken); this plugin is the visibility layer (on-demand, in-app).

## Screenshot

![Watchdog pane — degraded state](docs/watchdog-pane.png)

## Layout

```
watchdog_api.py            FastAPI service: /health, /status, /alerts,
                           /sources, /config, /run-check
watchdog_config.json       thresholds + watched sources (hot-reloaded)
state/                     runtime state, gitignored: alerts.json (transition
                           log), sources.json (watermark cursors)
desktop-plugin/            plugin.js — the desktop plugin (copy to
                           ~/.hermes/desktop-plugins/watchdog/)
docs/mockup.html           approved mockup
docs/watchdog-pane.png     screenshot of the pane (degraded state)
```

## Requirements

- Python 3.11+ (`fastapi`, `uvicorn[standard]`)
- A Hermes Agent install (the desktop app + `~/.hermes` layout)
- Hermes-LCM (for the LCM embedding-health check) — the check degrades gracefully if its script is absent

## Running the backend

```bash
git clone <this-repo> && cd watchdog
python3 -m venv .venv && .venv/bin/pip install fastapi "uvicorn[standard]"
.venv/bin/python -m uvicorn watchdog_api:app --host 127.0.0.1 --port 8766
```

### Configuration (env vars)

| Var | Default | Purpose |
|---|---|---|
| `WATCHDOG_HOST` | `127.0.0.1` | Bind address — keep it on a private interface |
| `WATCHDOG_PORT` | `8766` | Port |
| `HERMES_HOME` | `~/.hermes` | Where `scripts/`, `lcm.db`, `cron/jobs.json` live |
| `DAILY_DISK_ALERT_PCT` | `80` | Disk threshold |

Read-only by design — `/run-check` only re-runs checks; nothing here mutates. CORS is open because the desktop app renderer fetches cross-origin. **Add a token before ever exposing it wider than a private network.**

## Checks surfaced

| id | check | source |
|---|---|---|
| lcm | embedding health (coverage, provider, last embed) + db integrity + summary backlog | `lcm_health_check.py` + direct `lcm.db` reads |
| disk | worst usage vs threshold (`thresholds.disk_pct`, default 80) | `df` |
| mem | free + loadavg (informational) | `free`, `/proc/loadavg` |
| processes | ollama, gateway running | `pgrep` |
| cron | jobs.json audit: failures + staleness per cadence | `~/.hermes/cron/jobs.json` |

## Alert history

`update_alerts()` runs on every `/status` poll and records *transitions*, not snapshots: opening an alert when a check worsens, resolving it on recovery, updating severity/message on escalation. The **first run is a silent baseline** — pre-existing problems never spam the history. Persisted atomically to `state/alerts.json`; capped at `alerts.max_kept` (default 50). Active (unresolved) alerts always sort above resolved.

## Watched sources

Configured in `watchdog_config.json` → `sources[]`. Each source keeps a watermark cursor in `state/sources.json`; `/sources` reports new-item counts since the watermark. RSS uses the item guid (falling back to link, then title); GitHub compares the latest release tag. Fetch failures degrade that source only — they never flip the overall chip. `kind`: `rss` (url) or `github` (repo + ref, default `releases/latest`).

## Plugin install

1. Copy `desktop-plugin/plugin.js` to `~/.hermes/desktop-plugins/watchdog/plugin.js` (folder name == plugin id).
2. Set `WATCHDOG_BACKEND_URL` at the top of `plugin.js` to your backend — the plugin's own FastAPI status service (`http://127.0.0.1:8766` if Hermes runs on this machine, or your Tailscale/LAN IP for a remote host). This is **not** the Hermes gateway, **not** the Hermes web dashboard, and **not** the OpenAI-compatible API server.
3. In the desktop app: **⌘K → Reload desktop plugins** (hot-reload usually picks it up).

## License

MIT
