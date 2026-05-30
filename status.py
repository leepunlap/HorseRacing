"""status.py — live, in-memory process/task status layer.

This is the structured-telemetry counterpart to utils.emit (which carries
free-text log lines). Here we model *processes* and *tasks* so the dashboard can
show: which processes are running and for how long, a to-do list of tasks that
strikes items off as they complete, progress bars + ETAs, and errors — all live.

Design (live-only, no DB — state resets on app restart, by design):

    producer (any process)            app process
    ─────────────────────             ───────────
    status.task_start(...)  ─PUBLISH──►  consume_forever()  ──► sio.emit('status')
    status.task_step(...)    hr:status   registry.apply(ev)        (browsers)
    status.heartbeat(...)                registry.snapshot()  ──► GET /api/status

Producers run anywhere — an in-process asyncio loop or a standalone cron
script. They only PUBLISH to Redis (fire-and-forget, never raise). The app is
the single consumer: it keeps the authoritative in-memory Registry and forwards
each event to the browsers over Socket.IO.

Event shapes (all JSON dicts on the `hr:status` Redis channel):

    process:  {kind:'process', event:'up'|'beat'|'down', name, ptype, activity?, pid?, ts}
    task:     {kind:'task', event:'start', task_id, process, title, total?, group?, ts}
              {kind:'task', event:'step',  task_id, done, total?, msg?, ts}
              {kind:'task', event:'done',  task_id, msg?, ts}
              {kind:'task', event:'error', task_id, error, ts}
"""

from __future__ import annotations

import json
import os
import secrets
import time
from typing import Any, Optional

CHANNEL = "hr:status"
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

# How long after the last heartbeat a *loop* process is considered stalled.
# Loops beat ~every 60s; allow 2.5x slack before flagging.
LOOP_STALE_S = 150
# Keep at most this many finished (done/error) tasks in the snapshot.
MAX_FINISHED = 60
# Drop finished tasks older than this many seconds.
FINISHED_TTL_S = 1800


# ──────────────────────────────────────────────────────────────────────────
#  Producer API  (safe to call from any process; never raises)
# ──────────────────────────────────────────────────────────────────────────

_redis = None


def _client():
    global _redis
    if _redis is None:
        import redis  # lazy: keeps import cost off scripts that never emit
        _redis = redis.Redis.from_url(REDIS_URL)
    return _redis


def _publish(event: dict) -> bool:
    event.setdefault("ts", time.time())
    try:
        _client().publish(CHANNEL, json.dumps(event, ensure_ascii=False, default=str))
        return True
    except Exception:
        return False  # telemetry must never break the caller


# ----- process lifecycle -----

def process_up(name: str, ptype: str = "loop", activity: str = "", pid: Optional[int] = None) -> None:
    """Announce a process is alive. ptype: 'loop' (long-lived) | 'oneshot'."""
    _publish({"kind": "process", "event": "up", "name": name, "ptype": ptype,
              "activity": activity, "pid": pid if pid is not None else os.getpid()})


def heartbeat(name: str, activity: str = "") -> None:
    """Periodic liveness ping; `activity` is the human 'what it's doing now' line."""
    _publish({"kind": "process", "event": "beat", "name": name, "activity": activity})


def process_down(name: str, activity: str = "stopped") -> None:
    """Announce a clean stop (graceful shutdown)."""
    _publish({"kind": "process", "event": "down", "name": name, "activity": activity})


# ----- task lifecycle -----

def task_start(process: str, title: str, total: Optional[int] = None,
               task_id: Optional[str] = None, group: Optional[str] = None) -> str:
    """Begin a task; returns its id (pass to task_step/done/error).

    `total` enables a progress bar + ETA. `group` lets the UI cluster related
    tasks (e.g. all checks of one integrity run) under one heading.
    """
    tid = task_id or f"{process}.{secrets.token_hex(5)}"
    _publish({"kind": "task", "event": "start", "task_id": tid, "process": process,
              "title": title, "total": total, "group": group or process})
    return tid


def task_step(task_id: str, done: Optional[int] = None, advance: int = 1,
              total: Optional[int] = None, msg: Optional[str] = None) -> None:
    """Advance a task. Pass absolute `done`, or omit it to advance by `advance`."""
    ev: dict[str, Any] = {"kind": "task", "event": "step", "task_id": task_id}
    if done is not None:
        ev["done"] = done
    else:
        ev["advance"] = advance
    if total is not None:
        ev["total"] = total
    if msg is not None:
        ev["msg"] = msg
    _publish(ev)


def task_done(task_id: str, msg: Optional[str] = None) -> None:
    _publish({"kind": "task", "event": "done", "task_id": task_id, "msg": msg})


def task_error(task_id: str, error: str) -> None:
    _publish({"kind": "task", "event": "error", "task_id": task_id, "error": str(error)})


# ──────────────────────────────────────────────────────────────────────────
#  Registry  (authoritative state — only meaningful in the app process)
# ──────────────────────────────────────────────────────────────────────────

class Registry:
    """Folds the event stream into a current snapshot of processes + tasks."""

    def __init__(self) -> None:
        self.processes: dict[str, dict] = {}
        self.tasks: dict[str, dict] = {}

    # -- event folding --
    def apply(self, ev: dict) -> None:
        kind = ev.get("kind")
        if kind == "process":
            self._apply_process(ev)
        elif kind == "task":
            self._apply_task(ev)

    def _apply_process(self, ev: dict) -> None:
        name = ev.get("name")
        if not name:
            return
        now = ev.get("ts", time.time())
        p = self.processes.get(name)
        if ev["event"] == "up" or p is None:
            p = self.processes.setdefault(name, {
                "name": name, "started_at": now, "error_count": 0,
            })
            if ev["event"] == "up":
                p["started_at"] = now
                p["stopped"] = False
        p["ptype"] = ev.get("ptype", p.get("ptype", "loop"))
        p["last_beat"] = now
        if ev.get("activity"):
            p["activity"] = ev["activity"]
        if ev.get("pid") is not None:
            p["pid"] = ev["pid"]
        if ev["event"] == "down":
            p["stopped"] = True
            p["activity"] = ev.get("activity", "stopped")

    def _apply_task(self, ev: dict) -> None:
        tid = ev.get("task_id")
        if not tid:
            return
        now = ev.get("ts", time.time())
        if ev["event"] == "start":
            self.tasks[tid] = {
                "task_id": tid, "process": ev.get("process", "?"),
                "title": ev.get("title", tid), "group": ev.get("group") or ev.get("process", "?"),
                "state": "running", "done": 0, "total": ev.get("total"),
                "started_at": now, "updated_at": now, "finished_at": None,
                "msg": None, "error": None,
            }
            self._prune()
            return
        t = self.tasks.get(tid)
        if t is None:
            # step/done/error arriving before start (e.g. app restarted mid-task):
            # synthesise a minimal record so nothing is lost.
            t = self.tasks[tid] = {
                "task_id": tid, "process": "?", "title": tid, "group": "?",
                "state": "running", "done": 0, "total": None,
                "started_at": now, "updated_at": now, "finished_at": None,
                "msg": None, "error": None,
            }
        t["updated_at"] = now
        if ev["event"] == "step":
            if "done" in ev:
                t["done"] = ev["done"]
            else:
                t["done"] = (t.get("done") or 0) + ev.get("advance", 1)
            if ev.get("total") is not None:
                t["total"] = ev["total"]
            if ev.get("msg"):
                t["msg"] = ev["msg"]
        elif ev["event"] == "done":
            t["state"] = "done"
            t["finished_at"] = now
            if t.get("total"):
                t["done"] = t["total"]
            if ev.get("msg"):
                t["msg"] = ev["msg"]
        elif ev["event"] == "error":
            t["state"] = "error"
            t["finished_at"] = now
            t["error"] = ev.get("error", "error")
            p = self.processes.get(t["process"])
            if p is not None:
                p["error_count"] = p.get("error_count", 0) + 1
        self._prune()

    def _prune(self) -> None:
        """Bound memory: drop oldest finished tasks beyond MAX_FINISHED / TTL."""
        now = time.time()
        finished = [t for t in self.tasks.values() if t["state"] in ("done", "error")]
        # TTL drop
        for t in finished:
            if t["finished_at"] and now - t["finished_at"] > FINISHED_TTL_S:
                self.tasks.pop(t["task_id"], None)
        # count cap (keep newest)
        finished = sorted(
            [t for t in self.tasks.values() if t["state"] in ("done", "error")],
            key=lambda t: t.get("finished_at") or 0,
        )
        excess = len(finished) - MAX_FINISHED
        for t in finished[:max(0, excess)]:
            self.tasks.pop(t["task_id"], None)

    # -- snapshot for the UI --
    def snapshot(self) -> dict:
        now = time.time()
        procs = []
        for p in self.processes.values():
            last = p.get("last_beat", p.get("started_at", now))
            stale = (now - last) > LOOP_STALE_S
            if p.get("stopped"):
                pstate = "stopped"
            elif p.get("ptype") == "oneshot":
                pstate = "running" if any(
                    t["process"] == p["name"] and t["state"] == "running"
                    for t in self.tasks.values()
                ) else "idle"
            else:
                pstate = "stalled" if stale else "running"
            procs.append({
                "name": p["name"], "ptype": p.get("ptype", "loop"),
                "state": pstate, "running": pstate == "running",
                "started_at": p.get("started_at"),
                "uptime_s": round(now - p.get("started_at", now)),
                "last_beat_s_ago": round(now - last),
                "activity": p.get("activity", ""),
                "error_count": p.get("error_count", 0),
                "pid": p.get("pid"),
            })
        procs.sort(key=lambda x: x["name"])

        tasks = [self._task_view(t, now) for t in self.tasks.values()]
        # running first, then pending, then most-recently-finished
        order = {"running": 0, "pending": 1, "done": 2, "error": 2}
        tasks.sort(key=lambda t: (order.get(t["state"], 3),
                                  -(t["finished_at"] or t["started_at"] or 0)))
        return {"now": now, "processes": procs, "tasks": tasks}

    @staticmethod
    def _task_view(t: dict, now: float) -> dict:
        done = t.get("done") or 0
        total = t.get("total")
        started = t.get("started_at") or now
        elapsed = (t.get("finished_at") or now) - started
        pct = None
        eta_s = None
        eta_ts = None
        if total and total > 0:
            pct = max(0.0, min(1.0, done / total))
            if t["state"] == "running" and done > 0:
                rate = elapsed / done           # seconds per unit
                remaining = max(0, total - done)
                eta_s = round(rate * remaining)
                eta_ts = now + eta_s
        return {
            "task_id": t["task_id"], "process": t["process"], "group": t.get("group"),
            "title": t["title"], "state": t["state"],
            "done": done, "total": total, "pct": pct,
            "started_at": started, "finished_at": t.get("finished_at"),
            "elapsed_s": round(elapsed), "eta_s": eta_s, "eta_ts": eta_ts,
            "msg": t.get("msg"), "error": t.get("error"),
        }


# Singleton registry the app consumer folds events into.
registry = Registry()


# ──────────────────────────────────────────────────────────────────────────
#  Consumer  (runs inside the app; subscribes to Redis, forwards to Socket.IO)
# ──────────────────────────────────────────────────────────────────────────

async def consume_forever(sio, event_name: str = "status") -> None:
    """Subscribe to hr:status; fold each event into the registry and push it to
    browsers via sio.emit(event_name, ev). Resilient to Redis blips.

    Uses get_message(timeout=…) rather than listen(): an idle period returns
    None (not an exception), so we DON'T tear down the subscription — and thus
    don't lose events in a reconnect window — just because no event arrived.
    Only a genuine connection failure triggers a reconnect.
    """
    import asyncio
    import redis.asyncio as aioredis
    from redis import exceptions as redis_exc

    while True:
        try:
            r = aioredis.from_url(REDIS_URL)
            pubsub = r.pubsub()
            await pubsub.subscribe(CHANNEL)
            print(f"[status] subscribed to {CHANNEL}", flush=True)
            while True:
                try:
                    message = await pubsub.get_message(
                        ignore_subscribe_messages=True, timeout=5.0)
                except (redis_exc.TimeoutError, asyncio.TimeoutError):
                    continue   # idle — normal, keep the subscription alive
                if not message:
                    continue
                data = message.get("data")
                if data is None:
                    continue
                try:
                    ev = json.loads(data)
                except Exception:
                    continue
                registry.apply(ev)
                try:
                    await sio.emit(event_name, ev)
                except Exception:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[status] consumer error: {exc}; reconnecting in 2s", flush=True)
            try:
                await asyncio.sleep(2)
            except asyncio.CancelledError:
                raise
