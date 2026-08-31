#!/usr/bin/env python3
"""LCM embedding health check (provider-agnostic).

Checks the hermes-lcm embedding stack and prints a compact status report.
Exit 0 = healthy, 1 = degraded (needs attention). Used by the daily cron.

Checks:
  1. Embedding provider reachable + model present (reads ACTIVE provider from
     lcm.db, not a hardcoded default — so it validates Voyage today and
     auto-adapts when you migrate to Ollama).
  2. Active embedding profiles (summary + chunk)
  3. Vector coverage: summary vectors vs summary_nodes, chunk vectors vs chunk_meta
  4. In-flight/uncertain backfill rows (stuck leases or provider errors)
  5. Last backfill timestamps (staleness)

Health signals are printed to stdout; the daily wrapper decides whether to
surface them to the user (silent-unless-broken).
"""
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

LCM_DB = os.path.expanduser("~/.hermes/lcm.db")


def _load_dotenv(path):
    """Minimal .env loader (no external deps). Explicit env wins over file."""
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except OSError:
        pass


# The script runs under the watchdog systemd service and the cron scheduler,
# neither of which exports ~/.hermes/.env. Load it so VOYAGE_API_KEY and the
# LCM_* flags reflect the user's real configuration regardless of caller.
_load_dotenv(os.path.expanduser("~/.hermes/.env"))

problems = []
notes = []


def line(label, ok, detail=""):
    status = "OK " if ok else "BAD"
    print(f"[{status}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        problems.append(f"{label}: {detail}")


def get_provider_config():
    """Read the ACTIVE embedding profile from lcm.db (provider/model/task).
    Returns (provider, model_name, api_base_hint). Falls back to env on error."""
    try:
        db = sqlite3.connect(f"file:{LCM_DB}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT provider, model_name, task FROM lcm_embedding_profile "
            "WHERE active = 1"
        ).fetchall()
        db.close()
        if rows:
            # Prefer summary profile for provider/model identity.
            summary = next((r for r in rows if r["task"] == "summary"), rows[0])
            return summary["provider"], summary["model_name"], rows
        return None, None, None
    except sqlite3.Error:
        return None, None, None


PROVIDER, MODEL, ALL_PROFILES = get_provider_config()
if not PROVIDER:
    PROVIDER = os.environ.get("LCM_EMBEDDING_PROVIDER", "voyage")
    MODEL = os.environ.get("LCM_EMBEDDING_MODEL", "voyage-4-lite")

# Embeddings intentionally disabled (LCM_EMBEDDINGS_ENABLED=false): the
# embedding stack being off is the CONFIGURED state, not a fault. Report it as
# a note and exit healthy so the watchdog pane + daily cron don't cry wolf
# about stale last-embed timestamps, missing vectors, or the API key while the
# user deliberately runs without embedding maintenance.
EMBEDDINGS_ENABLED = os.environ.get(
    "LCM_EMBEDDINGS_ENABLED", "false"
).strip().lower() in ("1", "true", "yes", "on")
if not EMBEDDINGS_ENABLED:
    print("[--] embedding stack disabled (LCM_EMBEDDINGS_ENABLED=false) — "
          "skipping embedding health checks")
    print()
    print("STATUS: HEALTHY")
    sys.exit(0)


def check_provider_health(provider, model):
    """Reachability + model-present check for the active provider."""
    if provider == "ollama":
        base = os.environ.get("LCM_OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
        try:
            with urllib.request.urlopen(f"{base}/api/tags", timeout=10) as r:
                tags = json.loads(r.read())
            models = [m.get("name", "") for m in tags.get("models", [])]
            ok = any(model in m for m in models)
            line("ollama daemon", True, f"{len(models)} models loaded")
            line(f"model {model}", ok, "present" if ok else "MISSING — run: ollama pull " + model)
        except Exception as exc:
            line("ollama daemon", False, f"unreachable: {exc}")
    elif provider == "voyage":
        # Voyage is a remote API; reachability is implied by vector coverage and
        # the API key presence. We can't cheaply ping it here without burning a
        # request — so flag only if the API key is absent.
        key = os.environ.get("VOYAGE_API_KEY")
        if not key:
            line("voyage api key", False, "VOYAGE_API_KEY not set in ~/.hermes/.env")
        else:
            line("voyage api key", True, "set")
    else:
        line(f"provider {provider}", True, f"custom provider — no reachability probe defined")


# 1. Provider reachability + model
check_provider_health(provider=PROVIDER, model=MODEL)

# 2-4. DB checks
summary_nodes = summary_vecs = chunk_meta = chunk_vecs = inflight = 0
try:
    db = sqlite3.connect(f"file:{LCM_DB}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row

    profiles = db.execute(
        "SELECT provider, model_name, task, active FROM lcm_embedding_profile"
    ).fetchall()
    active = [p for p in profiles if p["active"]]
    summary_active = any(p["task"] == "summary" for p in active)
    chunk_active = any(p["task"] == "chunk" for p in active)
    line("active profiles", bool(active),
         ", ".join(f"{p['provider']}/{p['model_name']}:{p['task']}" for p in active) or "NONE")
    line("summary profile active", summary_active)
    line("chunk profile active", chunk_active)

    summary_nodes = db.execute(
        "SELECT COUNT(*) FROM summary_nodes WHERE depth = 0"
    ).fetchone()[0]
    row = db.execute(
        "SELECT COUNT(*) c FROM lcm_embedding_vectors v JOIN lcm_embedding_profile p"
        " ON v.identity_hash = p.identity_hash WHERE p.active = 1 AND p.task = 'summary'"
    ).fetchone()
    summary_vecs = row["c"] if row else 0

    chunk_meta = db.execute("SELECT COUNT(*) FROM lcm_chunk_meta").fetchone()[0]
    row = db.execute(
        "SELECT COUNT(*) c FROM lcm_chunk_vectors v JOIN lcm_embedding_profile p"
        " ON v.identity_hash = p.identity_hash WHERE p.active = 1 AND p.task = 'chunk'"
    ).fetchone()
    chunk_vecs = row["c"] if row else 0

    line("summary coverage", summary_vecs >= summary_nodes,
         f"{summary_vecs}/{summary_nodes}")
    line("chunk coverage", chunk_vecs >= chunk_meta,
         f"{chunk_vecs}/{chunk_meta}")

    inflight_rows = db.execute(
        "SELECT state, last_error, COUNT(*) c FROM lcm_embedding_backfill_inflight"
        " GROUP BY state, last_error"
    ).fetchall()
    inflight = sum(r["c"] for r in inflight_rows)
    bad_inflight = [r for r in inflight_rows if r["state"] == "uncertain" or r["last_error"]]
    line("inflight backfill rows", not bad_inflight,
         "; ".join(f"{r['state']} x{r['c']} {r['last_error'] or ''}".strip() for r in inflight_rows) or "none")

    newest = db.execute(
        "SELECT MAX(embedded_at) m FROM lcm_embedding_meta"
    ).fetchone()
    row2 = db.execute("SELECT MAX(embedded_at) m FROM lcm_chunk_meta").fetchone()
    newest = max([x for x in (newest["m"] if newest else None, row2["m"] if row2 else None) if x] or [None])
    if newest:
        dt = datetime.fromisoformat(newest).astimezone(timezone.utc)
        age_days = (datetime.now(timezone.utc) - dt).total_seconds() / 86400
        line("last embed", age_days < 2, f"{newest} ({age_days:.1f}d ago)")
    else:
        line("last embed", False, "no embeddings found")

    db.close()
except sqlite3.Error as exc:
    line("lcm.db", False, f"unreadable: {exc}")
except Exception as exc:
    line("lcm.db", False, f"error: {exc}")

print()
if problems:
    print(f"STATUS: DEGRADED ({len(problems)} problem(s))")
    sys.exit(1)
print("STATUS: HEALTHY")
sys.exit(0)
