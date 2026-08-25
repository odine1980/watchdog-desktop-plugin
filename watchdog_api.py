#!/usr/bin/env python3
"""Watchdog status API — the visibility layer for the watchdog desktop plugin.

Mirrors the checks in the daily cron watchdog script
(SCRIPTS/lcm_daily_check.py, the source of truth — this service does not
modify it):

  - LCM embedding health -> shells out to the SAME lcm_health_check.py so the
    pane and the cron always agree (one source of truth)
  - Disk usage           -> df, threshold 80% (DAILY_DISK_ALERT_PCT env)
  - Memory / load        -> free + /proc/loadavg (informational)
  - Key processes        -> pgrep ollama, gateway
  - Cron health          -> HERMES_HOME/cron/jobs.json audit (failures +
    staleness)

plus two read-only lcm.db extras: SQLite integrity and summary backlog.

Read-only by design: /run-check only RE-RUNS checks; nothing here mutates.
Bind via WATCHDOG_HOST/WATCHDOG_PORT (default 127.0.0.1:8766) — keep it on a
private interface. CORS is open because the desktop app renderer fetches
cross-origin. Add a token before ever exposing it wider.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

HERMES_HOME = os.path.expanduser(os.environ.get("HERMES_HOME", "~/.hermes"))
SCRIPTS = os.path.join(HERMES_HOME, "scripts")
LCM_DB = os.path.join(HERMES_HOME, "lcm.db")
CRON_JSON = os.path.join(HERMES_HOME, "cron", "jobs.json")
LCM_HEALTH = os.path.join(SCRIPTS, "lcm_health_check.py")
DISK_THRESHOLD_PCT = int(os.environ.get("DAILY_DISK_ALERT_PCT", "80"))
WANTED_PROCESSES = ["ollama", "gateway"]
SELF_SCRIPT = "lcm_daily_check.py"  # cron script to skip in the staleness audit

# ---------------- config + state (Phase 3: thresholds, alerts, sources) ------

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_JSON = os.path.join(PROJECT_DIR, "watchdog_config.json")
STATE_DIR = os.path.join(PROJECT_DIR, "state")
ALERTS_JSON = os.path.join(STATE_DIR, "alerts.json")
SOURCES_JSON = os.path.join(STATE_DIR, "sources.json")

DEFAULT_CONFIG = {
    "thresholds": {
        "disk_pct": int(os.environ.get("DAILY_DISK_ALERT_PCT", "80")),
        "backlog_s": 6 * 3600,
    },
    "alerts": {"max_kept": 50},
    "sources": [],
}

# user-agent for RSS / GitHub fetches (GitHub API requires a UA)
UA = "watchdog/0.2 (+tailscale-only)"

app = FastAPI(title="Watchdog status API", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

LINE_RE = re.compile(r"^\[(OK|BAD)\s*\]\s*(.+?)(?:\s+—\s+(.*))?$")


def _run(cmd, timeout=60):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, f"TIMEOUT after {timeout}s"
    except Exception as exc:
        return 2, str(exc)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_config() -> dict:
    """Config with defaults; re-read per request so edits hot-reload."""
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy of defaults
    try:
        with open(CONFIG_JSON) as f:
            user = json.load(f)
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    except Exception:
        pass  # missing/broken config -> defaults
    return cfg


def _read_json(path: str, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _write_json_atomic(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


# ---------------- LCM (shells out to the shared health script) ----------------

def check_lcm() -> dict:
    rc, out = _run([sys.executable, LCM_HEALTH], timeout=90)
    details = []
    for ln in out.splitlines():
        m = LINE_RE.match(ln)
        if m:
            status = "ok" if m.group(1) == "OK" else "critical"
            details.append({
                "status": status,
                "label": m.group(2).strip(),
                "detail": (m.group(3) or "").strip(),
            })
    status_line = next(
        (ln.strip() for ln in out.splitlines() if ln.startswith("STATUS:")),
        f"exit {rc}",
    )
    state = "ok" if rc == 0 else "degraded"
    summary = status_line.replace("STATUS: ", "")

    # Extras read directly from lcm.db (read-only, never mutated).
    try:
        db = sqlite3.connect(f"file:{LCM_DB}?mode=ro", uri=True)
        cur = db.cursor()
        integrity = cur.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            state = "degraded"
        details.append({"status": "ok" if integrity == "ok" else "critical",
                        "label": "db integrity", "detail": integrity})
        max_msg = cur.execute("SELECT MAX(timestamp) FROM messages").fetchone()[0]
        max_sum = cur.execute("SELECT MAX(latest_at) FROM summary_nodes").fetchone()[0]
        backlog_s = max(0, int((max_msg or 0) - (max_sum or 0)))
        backlog_thr = load_config()["thresholds"]["backlog_s"]
        if backlog_s > backlog_thr:
            state = "degraded"
        details.append({
            "status": "ok" if backlog_s <= backlog_thr else "degraded",
            "label": "summary backlog",
            "detail": _human_duration(backlog_s),
        })
        db.close()
    except sqlite3.Error as exc:
        state = "degraded"
        details.append({"status": "critical", "label": "lcm.db", "detail": str(exc)})

    return {"id": "lcm", "name": "LCM embedding health", "state": state,
            "summary": summary, "details": details,
            "available": os.path.exists(LCM_HEALTH) and os.path.exists(LCM_DB)}


def _human_duration(s: int) -> str:
    s = int(s)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60}s"
    return f"{s // 3600}h {(s % 3600) // 60}m"


# ---------------- Systems (mirrors lcm_daily_check.py thresholds) -------------

def check_disk() -> dict:
    thr = load_config()["thresholds"]["disk_pct"]
    rc, out = _run(["df", "-h", "-x", "tmpfs", "-x", "devtmpfs", "-x", "overlay"], timeout=20)
    over, worst = [], 0
    if rc == 0:
        for ln in out.strip().splitlines()[1:]:
            parts = ln.split()
            if len(parts) >= 5 and parts[4].endswith("%"):
                pct = int(parts[4][:-1])
                worst = max(worst, pct)
                if pct >= thr:
                    over.append({"status": "critical", "label": parts[5],
                                 "detail": f"{parts[4]} used of {parts[1]}"})
    else:
        over.append({"status": "critical", "label": "df", "detail": out.strip()[:200]})
    state = "critical" if over else "ok"
    return {"id": "disk", "name": "Disk", "state": state,
            "summary": f"worst {worst}% (threshold {thr}%)",
            "details": over}


def check_mem_load() -> dict:
    _, free = _run(["free", "-h"], timeout=20)
    _, load = _run(["cat", "/proc/loadavg"], timeout=20)
    mem_line = free.splitlines()[1] if free else "n/a"
    loads = load.strip().split()[:3] if load else ["n/a", "n/a", "n/a"]
    return {"id": "mem", "name": "Memory / Load", "state": "ok",
            "summary": f"mem: {mem_line} | load: {', '.join(loads)}",
            "details": []}


def check_processes() -> dict:
    missing = []
    for proc in WANTED_PROCESSES:
        r = subprocess.run(["pgrep", "-f", proc], capture_output=True, timeout=20)
        if r.returncode != 0:
            missing.append(proc)
    state = "critical" if missing else "ok"
    return {"id": "processes", "name": "Key processes", "state": state,
            "summary": ("missing: " + ", ".join(missing)) if missing else ", ".join(WANTED_PROCESSES) + " running",
            "details": [{"status": "critical", "label": p, "detail": "not running"} for p in missing]}


# ---------------- Cron health (mirrors lcm_daily_check.py audit) --------------

def check_cron_health() -> dict:
    issues = []
    try:
        with open(CRON_JSON) as f:
            jobs = json.load(f)["jobs"]
    except Exception as exc:
        return {"id": "cron", "name": "Cron health", "state": "critical",
                "summary": f"cannot read {CRON_JSON}", "details": [
                    {"status": "critical", "label": "jobs.json", "detail": str(exc)}]}

    now = datetime.now(timezone.utc)
    for j in jobs:
        if not j.get("enabled") or j.get("state") == "paused":
            continue
        if j.get("script") == SELF_SCRIPT:
            continue
        name = j.get("name") or j.get("id")
        if j.get("last_status") not in (None, "ok"):
            issues.append({"status": "critical", "label": name,
                           "detail": f"last_status={j.get('last_status')} err={j.get('last_error')}"})
        if j.get("last_error"):
            issues.append({"status": "critical", "label": name,
                           "detail": f"last_error={j.get('last_error')}"})
        if j.get("last_delivery_error"):
            issues.append({"status": "critical", "label": name,
                           "detail": f"delivery_error={j.get('last_delivery_error')}"})
        if j.get("failure_streak"):
            issues.append({"status": "critical", "label": name,
                           "detail": f"failure_streak={j.get('failure_streak')}"})
        lr = j.get("last_run_at")
        if lr:
            try:
                last = datetime.fromisoformat(str(lr).replace("Z", "+00:00"))
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                expr = j.get("schedule") or ""
                if isinstance(expr, dict):
                    expr = expr.get("expr", "")
                cadence_days = _cadence_from_expr(str(expr))
                if last < now - timedelta(days=cadence_days):
                    issues.append({"status": "critical", "label": name,
                                   "detail": f"stale — last ran {last.isoformat()} ({cadence_days}d cadence)"})
            except Exception:
                pass

    state = "critical" if issues else "ok"
    checked = sum(1 for j in jobs if j.get("enabled") and j.get("state") != "paused"
                  and j.get("script") != SELF_SCRIPT)
    return {"id": "cron", "name": "Cron health", "state": state,
            "summary": f"{checked} job(s) checked, {len(issues)} problem(s)",
            "details": issues}


def _cadence_from_expr(expr: str) -> int:
    """Best-effort cadence (days) from a cron expr — mirrors the daily script."""
    try:
        f = expr.split()
        if len(f) >= 5:
            dom, mon = f[2], f[3]
            if dom != "*" or mon != "*":
                return 32
            if f[4] != "*":
                return 8
            h, d = f[1], f[0]
            if d.startswith("*/"):
                return max(1, int(d[2:]) // 24 or 1)
            if h.startswith("*/"):
                return max(1, int(h[2:]))
            if h != "*":
                return 1
            if d != "*":
                return 1
            return 1
        return 1
    except Exception:
        return 1


# ---------------- Alert history (transition tracking) ------------------------

SEV_ORDER = {"ok": 0, "degraded": 1, "critical": 2}


def update_alerts(checks: list[dict]) -> list[dict]:
    """Record check state transitions as alerts (open on worsening, resolve on
    recovery). First run with no prior state is a silent baseline — pre-existing
    problems never spam the history. Idempotent: persists prev_checks + alerts
    to state/alerts.json (atomic write)."""
    cfg = load_config()
    max_kept = int(cfg["alerts"]["max_kept"])
    state = _read_json(ALERTS_JSON, {"prev_checks": None, "alerts": []})
    prev = state.get("prev_checks") or {}
    alerts = state.get("alerts") or []
    by_check = {a["check"]: a for a in alerts if not a.get("resolved_at")}

    for c in checks:
        cid, cur = c["id"], c["state"]
        p = prev.get(cid)
        if p is None:
            continue  # baseline
        if p == cur:
            continue
        if cur == "ok":
            a = by_check.get(cid)
            if a:
                a["resolved_at"] = _now_iso()
        elif p == "ok" or SEV_ORDER[cur] > SEV_ORDER.get(p, 0):
            a = by_check.get(cid)
            if a is None:
                alerts.append({
                    "id": f"{cid}-{time.time_ns()}",
                    "check": cid,
                    "name": c["name"],
                    "severity": cur,
                    "message": c["summary"],
                    "opened_at": _now_iso(),
                    "resolved_at": None,
                })
                by_check[cid] = a
            else:
                a["severity"] = cur
                a["message"] = c["summary"]

    # active (unresolved) alerts always sort above resolved; within a group,
    # newest first
    alerts.sort(key=lambda a: (
        0 if a.get("resolved_at") else 1,
        a.get("resolved_at") or a.get("opened_at") or "",
    ), reverse=True)
    if len(alerts) > max_kept:
        alerts = alerts[:max_kept]
    _write_json_atomic(ALERTS_JSON, {
        "prev_checks": {c["id"]: c["state"] for c in checks},
        "alerts": alerts,
    })
    return alerts


# ---------------- Watched sources (RSS + GitHub, watermark cursors) -----------

def _fetch(url: str, timeout: int = 10):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/rss+xml, application/atom+xml, application/json, */*",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", "replace"), dict(r.headers)


def _rss_items(body: str) -> list[str]:
    """First N item ids (guid > link > title) from RSS 2.0 or Atom."""
    root = ET.fromstring(body)
    ns = ""
    if root.tag.startswith("{"):
        ns = root.tag.split("}")[0] + "}"
    items = root.findall(f".//{ns}item")
    if not items:
        items = root.findall(".//{http://www.w3.org/2005/Atom}entry")
        ns = "{http://www.w3.org/2005/Atom}"
    out = []
    for it in items[:50]:
        guid = it.find(f"{ns}guid")
        key = guid.text if guid is not None else None
        if not key:
            link = it.find(f"{ns}link")
            key = link.text if link is not None else None
        if not key:
            title = it.find(f"{ns}title")
            key = (title.text if title is not None else "") or ""
        out.append(key)
    return out


def _fmt_epoch(ts) -> str:
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%H:%M UTC")
    except Exception:
        return "soon"


def check_sources() -> dict:
    """Poll each configured source, track a watermark cursor per source in
    state/sources.json, and report new-item counts. Failures degrade that
    source only (informational, never flips the overall chip)."""
    cfg = load_config()
    st = _read_json(SOURCES_JSON, {})
    sources = []
    for src in cfg["sources"]:
        sid = src["id"]
        prev = st.get(sid, {})
        entry = {
            "id": sid, "name": src["name"], "kind": src["kind"],
            "status": "ok", "new_count": 0,
            "watermark": prev.get("watermark"),
            "detail": None, "checked_at": _now_iso(),
        }
        try:
            if src["kind"] == "rss":
                _, body, _ = _fetch(src["url"])
                items = _rss_items(body)
                if not items:
                    raise ValueError("empty feed")
                wm = entry["watermark"]
                if wm is None:
                    entry["watermark"] = items[0]
                elif wm in items:
                    entry["new_count"] = items.index(wm)
                else:
                    # watermark rolled off the feed; count what's visible once,
                    # then advance so we don't re-count the same items forever
                    entry["new_count"] = len(items)
                    entry["watermark"] = items[0]
            elif src["kind"] == "github":
                _, body, _ = _fetch(
                    f"https://api.github.com/repos/{src['repo']}/{src.get('ref') or 'releases/latest'}"
                )
                data = json.loads(body)
                cur = data.get("tag_name") or data.get("name") or str(data.get("id", ""))
                wm = entry["watermark"]
                if wm is None:
                    entry["watermark"] = cur
                elif cur != wm:
                    entry["new_count"] = 1
                    entry["watermark"] = cur
        except urllib.error.HTTPError as exc:
            entry["status"] = "degraded"
            if exc.code == 403 and exc.headers.get("X-RateLimit-Remaining") == "0":
                entry["detail"] = (
                    "GitHub API rate limited (resets "
                    f"{_fmt_epoch(exc.headers.get('X-RateLimit-Reset'))})"
                )
            else:
                entry["detail"] = f"HTTP {exc.code} {exc.reason}"
        except Exception as exc:
            entry["status"] = "degraded"
            entry["detail"] = f"{type(exc).__name__}: {str(exc)[:160]}"
        sources.append(entry)
        st[sid] = {"watermark": entry["watermark"], "status": entry["status"],
                   "detail": entry["detail"]}
    _write_json_atomic(SOURCES_JSON, st)
    return {"generated_at": _now_iso(), "sources": sources}


# ---------------- Aggregation + endpoints ------------------------------------

def run_all() -> dict:
    checks = [check_lcm(), check_disk(), check_mem_load(), check_processes(), check_cron_health()]
    order = {"ok": 0, "degraded": 1, "critical": 2}
    worst = max(order[c["state"]] for c in checks)
    state = {0: "ok", 1: "degraded", 2: "critical"}[worst]
    stats = {}
    for c in checks:
        if c["id"] == "disk":
            m = re.search(r"worst (\d+)%", c["summary"])
            if m:
                stats["disk_pct"] = int(m.group(1))
        if c["id"] == "mem":
            stats["mem_summary"] = c["summary"]
        if c["id"] == "lcm":
            for d in c["details"]:
                if d["label"] == "summary backlog":
                    stats["lcm_backlog"] = d["detail"]
                if d["label"] == "db integrity":
                    stats["lcm_integrity"] = d["detail"]
    return {"generated_at": _now_iso(), "state": state, "checks": checks, "stats": stats}


class RunCheckRequest(BaseModel):
    check: str = "all"


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.get("/status")
def status() -> dict:
    data = run_all()
    data["alerts"] = update_alerts(data["checks"])
    return data


@app.get("/alerts")
def alerts() -> dict:
    st = _read_json(ALERTS_JSON, {"alerts": []})
    return {"generated_at": _now_iso(), "alerts": st.get("alerts", [])}


@app.get("/sources")
def sources() -> dict:
    return check_sources()


@app.get("/config")
def config() -> dict:
    cfg = load_config()
    return {"thresholds": cfg["thresholds"], "alerts": cfg["alerts"],
            "sources": cfg["sources"]}


@app.post("/run-check")
def run_check(_body: RunCheckRequest | None = None) -> dict:
    # v1: always re-runs everything; the body exists so the client's intent is
    # explicit and the endpoint is forward-compatible with per-check runs.
    data = run_all()
    data["alerts"] = update_alerts(data["checks"])
    return data


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("WATCHDOG_HOST", "127.0.0.1"),
        port=int(os.environ.get("WATCHDOG_PORT", "8766")),
    )
