# Changelog

All notable changes to the Watchdog desktop plugin + backend are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/) and [SemVer](https://semver.org/).

## [1.0.1] - 2026-08-31

### Fixed

- **False "degraded" from intentionally-disabled embeddings.** `lcm_health_check.py`
  flagged the LCM embedding stack as degraded when `last embed` was older than
  2 days, vector coverage was incomplete, or `VOYAGE_API_KEY` looked unset —
  even when the user had deliberately disabled embedding maintenance
  (`LCM_EMBEDDINGS_ENABLED=false`). The script now:
  - loads `~/.hermes/.env` itself (the watchdog systemd service and cron do
    not export it, so `VOYAGE_API_KEY` and the `LCM_*` flags were read as
    missing under those callers), and
  - exits healthy with an informational `[--] embedding stack disabled` note
    when `LCM_EMBEDDINGS_ENABLED` is false, skipping the embedding checks
    entirely. The configured state is not a fault.
- Resolved the stuck "LCM embedding health" alert that had been open since
  2026-08-27 and made the `/watchdog` pane show "degraded" with a 3-day-old
  "last embed" timestamp.

## [1.0.0] - 2026-08-25

### Added

- Public release of the Watchdog system: FastAPI status backend
  (`watchdog_api.py`) + Hermes desktop plugin (statusbar chip + `/watchdog`
  pane).
- Live checks: LCM embedding health, disk, memory/load, key processes, cron
  staleness audit.
- Watched sources (Hacker News RSS + hermes-agent GitHub releases) with
  watermark cursors; alert history with transition semantics.
- One-click LCM actions (status / diagnostics / rotate preview / compact /
  backup) gated on LCM availability, agent-mediated via the gateway `prompt.submit`
  RPC (never a second process writing lcm.db).
- Systemd user unit `watchdog-backend.service`, LAN/Tailscale reachable on
  `0.0.0.0:8766`.
