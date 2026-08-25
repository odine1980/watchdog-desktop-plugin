#!/usr/bin/env python3
"""Watchdog status API — the visibility layer for the watchdog desktop plugin.

Mirrors the checks in ~/.hermes/scripts/lcm_daily_check.py (the daily cron,
source of truth, UNTOUCHED — this service does not modify it):

  - LCM embedding health -> shells out to the SAME lcm_health_check.py so the
    pane and the cron always agree (one source of truth)
  - Disk usage           -> df, threshold 80% (DAILY_DISK_ALERT_PCT env)
  - Memory / load        -> free + /proc/loadavg (informational)
  - Key processes        -> pgrep ollama, gateway
  - Cron health          -> ~/.hermes/cron/jobs.json audit (failures + staleness)

plus two read-only lcm.db extras: SQLite integrity and summary backlog.

Read-only by design: /run-check only RE-RUNS checks; nothing here mutates.
Bound to the Tailscale IP only (127.0.0.1:8766), never 0.0.0.0.
CORS is open because the desktop app renderer fetches cross-origin; the
surface is Tailscale-only and read-only. Add a token if you ever expose it
wider.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

SCRIPTS = "~/.hermes/scripts"
LCM_DB = os.path.expanduser("~/.hermes/lcm.db")
CRON_JSON = "~/.hermes/cron/jobs.json"
LCM_HEALTH = f"{SCRIPTS}/lcm_health_check.py"
DISK_THRESHOLD_PCT = int(os.environ.get("DAILY_DISK_ALERT_PCT", "80"))
WANTED_PROCESSES = ["ollama", "gateway"]
SELF_SCRIPT = "lcm_daily_check.py"  # cron script to skip in the staleness audit

app = FastAPI(title="Watchdog status API", version="0.1.0")
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
        if backlog_s > 6 * 3600:
            state = "degraded"
        details.append({
            "status": "ok" if backlog_s <= 6 * 3600 else "degraded",
            "label": "summary backlog",
            "detail": _human_duration(backlog_s),
        })
        db.close()
    except sqlite3.Error as exc:
        state = "degraded"
        details.append({"status": "critical", "label": "lcm.db", "detail": str(exc)})

    return {"id": "lcm", "name": "LCM embedding health", "state": state,
            "summary": summary, "details": details}


def _human_duration(s: int) -> str:
    s = int(s)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60}s"
    return f"{s // 3600}h {(s % 3600) // 60}m"


# ---------------- Systems (mirrors lcm_daily_check.py thresholds) -------------

def check_disk() -> dict:
    rc, out = _run(["df", "-h", "-x", "tmpfs", "-x", "devtmpfs", "-x", "overlay"], timeout=20)
    over, worst = [], 0
    if rc == 0:
        for ln in out.strip().splitlines()[1:]:
            parts = ln.split()
            if len(parts) >= 5 and parts[4].endswith("%"):
                pct = int(parts[4][:-1])
                worst = max(worst, pct)
                if pct >= DISK_THRESHOLD_PCT:
                    over.append({"status": "critical", "label": parts[5],
                                 "detail": f"{parts[4]} used of {parts[1]}"})
    else:
        over.append({"status": "critical", "label": "df", "detail": out.strip()[:200]})
    state = "critical" if over else "ok"
    return {"id": "disk", "name": "Disk", "state": state,
            "summary": f"worst {worst}% (threshold {DISK_THRESHOLD_PCT}%)",
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
    return run_all()


@app.post("/run-check")
def run_check(_body: RunCheckRequest | None = None) -> dict:
    # v1: always re-runs everything; the body exists so the client's intent is
    # explicit and the endpoint is forward-compatible with per-check runs.
    return run_all()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8766)
