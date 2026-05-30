"""utils.emit — fire-and-forget progress telemetry for out-of-process jobs.

Any standalone script (a cron job, a one-off backfill, a scraper run outside
the app) can stream status into the live UI without importing the FastAPI app
or holding a WebSocket:

    from utils.emit import emit
    emit({"type": "scraper_log", "task": "integrity_check", "text": "healed 80 rows"})

How it works
────────────
The app runs a Socket.IO server whose client-manager is an AsyncRedisManager.
Here we create a *write-only* RedisManager pointed at the same Redis instance
and channel. `.emit()` PUBLISHes the message to Redis; the app's server is
SUBSCRIBEd and fans it out to every connected browser. No HTTP round-trip, and
the call succeeds (well, no-ops) even when the app is down.

Contract
────────
- The payload is the SAME dict shape the in-app loops broadcast. The UI routes
  on the `type` field ("scraper_log" | "lifecycle" | "race_settled" | ...).
- Emission is best-effort: any failure (Redis down, etc.) is swallowed so
  telemetry can never crash or slow the job that called it.
- The event name and Redis channel MUST match the server (see app.py:
  SOCKET_EVENT = "progress", AsyncRedisManager default channel "socketio").
"""

from __future__ import annotations

import os

# Must mirror app.py's `SOCKET_EVENT` and the AsyncRedisManager channel.
_EVENT = "progress"
_CHANNEL = "socketio"
_REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

# Lazily-created singleton so importing this module is cheap and a Redis outage
# at import time is harmless — we only touch Redis on the first emit().
_mgr = None


def _manager():
    global _mgr
    if _mgr is None:
        import socketio  # imported lazily to keep import-time cost off scripts
        _mgr = socketio.RedisManager(_REDIS_URL, channel=_CHANNEL, write_only=True)
    return _mgr


def emit(payload: dict) -> bool:
    """Publish one progress event to the UI. Returns True if handed to Redis.

    Never raises — returns False on any failure so callers can ignore it.
    """
    try:
        _manager().emit(_EVENT, payload, namespace="/")
        return True
    except Exception:
        return False


def log(task: str, text: str, **extra) -> bool:
    """Convenience for the common scraper_log shape: emit.log("cron", "started")."""
    payload = {"type": "scraper_log", "task": task, "text": text}
    payload.update(extra)
    return emit(payload)
