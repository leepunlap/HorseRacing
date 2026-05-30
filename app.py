#!/usr/bin/env python3
"""Horse Racing — FastAPI shell.

The v1 surface (model_config strategies, hypothesis catalogue, dashboard,
backtest jobs, horse/jockey/trainer pages, DeepSeek chat) was deleted in the
the minimum scaffolding the stack needs:

  * lifespan that owns the cron-like scheduler (`scheduler.py`) plus the
    odds poller (`scrapers.odds_poller`) and live decision-loop scheduler
    (`live.scheduler`)
  * a shared `scraper_job` dict that `api.register_actions` reads when
    wiring scrapers into scheduler actions
  * a `Broadcaster` for `/ws/progress` so scrapers + decision loop can stream
    log lines to the SPA
  * hardcoded-password auth (`/api/auth/{login,logout,check}`)
  * `/api/health` for liveness + startup-id reload polling
  * `/api/schedules` CRUD on top of `scheduler.py`'s storage
  * SPA index at `/`

All race/odds/feature/strategy/audit/kill-switch surface lives under
`/api/...` (mounted from `api.py`).
"""

from __future__ import annotations

import asyncio
import os
import secrets
import signal as _signal
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

import socketio
import uvicorn
from fastapi import (Depends, FastAPI, HTTPException, Query, Request,
                     WebSocket, WebSocketDisconnect)
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles


# ─── Paths + config ──────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
STATIC_DIR = BASE_DIR / "static"
DATA_DIR.mkdir(exist_ok=True)
STATIC_DIR.mkdir(exist_ok=True)


# ─── .env loader (secrets) ──────────────────────────────────────────────────
# Reads `BASE_DIR/.env` (gitignored) before any module reads os.environ.
# Holds DEEPSEEK_API_KEY for the betting.eval_reason narrator. We keep the
# parser minimal (no python-dotenv dependency); honours KEY=value and
# KEY="value with spaces".
def _load_env_file() -> None:
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        # Don't overwrite values already exported in the shell — env always wins.
        os.environ.setdefault(k, v)
_load_env_file()

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

HARDCODED_PASSWORD = "168888"
TOKENS: set[str] = set()

STARTUP_ID = f"{os.getpid()}-{int(datetime.now().timestamp())}"
SHUTTING_DOWN = False


# ─── Auth ────────────────────────────────────────────────────────────────────
security = HTTPBearer(auto_error=False)


def verify_token(credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)) -> bool:
    if credentials is None or credentials.credentials not in TOKENS:
        raise HTTPException(status_code=401, detail="未登入")
    return True


# ─── Socket.IO server (Redis-backed fan-out) ─────────────────────────────────
# The client_manager is an AsyncRedisManager: when we `sio.emit('progress', …)`
# the message goes through Redis, so ANY process that publishes to the same
# Redis channel reaches every connected browser. That's what lets cron scripts
# (via utils.emit.emit(), a write-only RedisManager) stream into the UI without
# importing the app or holding a WebSocket. See SOCKET_EVENT below.
SOCKET_EVENT = "progress"   # single event name; payload's `type` field routes in the UI

sio = socketio.AsyncServer(
    async_mode="asgi",
    client_manager=socketio.AsyncRedisManager(REDIS_URL),
    cors_allowed_origins="*",   # same-origin in practice; token-gated on connect
)


# ─── Broadcaster shim ────────────────────────────────────────────────────────
# Kept as a thin compatibility wrapper so every existing call site
# (`progress_broadcast.broadcast(payload)` in the scheduler, odds_poller, live
# decision loop, lifespan, GracefulServer) works unchanged. Fan-out now goes
# through Socket.IO + Redis instead of a private WebSocket client list.
class Broadcaster:
    """Compat wrapper: .broadcast(dict) → sio.emit over Redis."""

    async def broadcast(self, data: dict) -> None:
        try:
            await sio.emit(SOCKET_EVENT, data)
        except Exception as exc:   # telemetry must never crash a caller
            print(f"[broadcast] sio.emit failed: {exc}", flush=True)

    # Legacy no-ops — the old raw /ws/progress endpoint used these. Retained so
    # nothing breaks if a stray caller still invokes them.
    async def connect(self, ws: "WebSocket") -> None:
        await ws.accept()

    def disconnect(self, ws: "WebSocket") -> None:
        pass


progress_broadcast = Broadcaster()


# ─── Socket.IO connection handlers ───────────────────────────────────────────
@sio.event
async def connect(sid, environ, auth):
    """Token-gate the connection (same TOKENS set the REST API uses), then
    replay the on-connect state the old /ws/progress endpoint sent so a freshly
    loaded UI immediately knows lifecycle + in-flight scraper status."""
    token = (auth or {}).get("token") if isinstance(auth, dict) else None
    if not token or token not in TOKENS:
        raise socketio.exceptions.ConnectionRefusedError("unauthorized")

    await sio.emit(SOCKET_EVENT, {
        "type": "lifecycle",
        "phase": "draining" if SHUTTING_DOWN else "ready",
        "startup_id": STARTUP_ID, "pid": os.getpid(),
    }, to=sid)

    if scraper_job.get("active"):
        await sio.emit(SOCKET_EVENT, {
            "type": "scraper_start", "_resumed": True,
            "started_at": scraper_job["started_at"],
            "current_task": scraper_job["current_task"],
        }, to=sid)


# ─── Shared scraper-job slot ─────────────────────────────────────────────────
# api.register_actions reads this; the slot prevents two scrapers
# from running at once (the existing single-job convention).
scraper_job: dict = {
    "active": False,
    "stopping": False,
    "started_at": None,
    "current_task": None,
    "_proc": None,
}


# ─── Signal handlers ─────────────────────────────────────────────────────────
def _install_signal_handlers(loop: asyncio.AbstractEventLoop) -> None:
    """SIGHUP is a no-op now (was used to hot-reload the v1 hypothesis cache).
    Kept for forward-compat with the systemd unit."""

    def _on_hup() -> None:
        print("[signal] SIGHUP (no-op)", flush=True)

    try:
        loop.add_signal_handler(_signal.SIGHUP, _on_hup)
    except NotImplementedError:
        pass


_SUBPROC_KILL_TIMEOUT = 5.0


async def _terminate_subproc(proc, label: str) -> None:
    if proc is None or proc.returncode is not None:
        return
    try:
        proc.terminate()
        await asyncio.wait_for(proc.wait(), timeout=_SUBPROC_KILL_TIMEOUT)
        print(f"[shutdown] {label} subprocess exited cleanly", flush=True)
    except asyncio.TimeoutError:
        print(f"[shutdown] {label} timed out in {_SUBPROC_KILL_TIMEOUT}s — SIGKILL", flush=True)
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    except ProcessLookupError:
        pass


# ─── Lifespan ────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    _install_signal_handlers(asyncio.get_running_loop())

    # The cron-like scheduler. Action registry is shared with api.
    from scheduler import run_scheduler_loop, _action_registry

    # scraper actions
    try:
        from api import register_actions
        register_actions(_action_registry, scraper_job, lambda: SHUTTING_DOWN)
        print("[startup] scheduler actions registered", flush=True)
    except Exception as exc:
        print(f"[startup] register_actions failed: {exc}", flush=True)

    # Seed the post-mortem RCA tag catalog (idempotent)
    try:
        import sqlite3 as _sqlite3
        from betting.post_mortem import seed_tags as _pm_seed
        _c = _sqlite3.connect(BASE_DIR / "data" / "racing.db")
        _pm_seed(_c)
        _c.close()
        print("[startup] bet_tags seeded", flush=True)
    except Exception as exc:
        print(f"[startup] bet_tags seed failed: {exc}", flush=True)

    # In-process odds poller (T-60→T-0 every 30s on race days)
    odds_poller_task = None
    try:
        from scrapers.odds_poller import run_forever as _odds_run
        odds_poller_task = asyncio.create_task(_odds_run(progress_broadcast))
        print("[startup] odds_poller started", flush=True)
    except Exception as exc:
        print(f"[startup] odds_poller failed: {exc}", flush=True)

    # Live decision-loop scheduler (T-10 → T-0 per race; paper mode default)
    live_scheduler_task = None
    try:
        from live.scheduler import run_forever as _live_run
        live_scheduler_task = asyncio.create_task(_live_run(progress_broadcast))
        print("[startup] live scheduler started (paper mode default)", flush=True)
    except Exception as exc:
        print(f"[startup] live scheduler failed: {exc}", flush=True)

    scheduler_task = asyncio.create_task(
        run_scheduler_loop(progress_broadcast, lambda: SHUTTING_DOWN)
    )

    # Status consumer: subscribes to the hr:status Redis channel, folds events
    # into the in-memory registry, and forwards them to browsers as 'status'
    # events. This is what powers the live dashboard (GET /api/status snapshot
    # + real-time deltas).
    status_task = None
    try:
        import status as _status
        status_task = asyncio.create_task(_status.consume_forever(sio))
        print("[startup] status consumer started", flush=True)
    except Exception as exc:
        print(f"[startup] status consumer failed: {exc}", flush=True)

    print(f"[startup] horseracing app ready (PID {os.getpid()})", flush=True)
    await progress_broadcast.broadcast({
        "type": "lifecycle", "phase": "ready",
        "startup_id": STARTUP_ID, "pid": os.getpid(),
    })

    yield

    # ── Shutdown ─────────────────────────────────────────────────────────────
    global SHUTTING_DOWN
    SHUTTING_DOWN = True
    print("[shutdown] draining…", flush=True)
    try:
        await progress_broadcast.broadcast({
            "type": "lifecycle", "phase": "draining",
            "startup_id": STARTUP_ID,
        })
    except Exception:
        pass

    for task, label in (
        (scheduler_task, "scheduler"),
        (odds_poller_task, "odds_poller"),
        (live_scheduler_task, "live_scheduler"),
        (status_task, "status_consumer"),
    ):
        if task is None:
            continue
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    await _terminate_subproc(scraper_job.get("_proc"), "scraper")
    print("[shutdown] graceful shutdown complete", flush=True)


app = FastAPI(title="馬場分析 / HorseLab", lifespan=lifespan)


# ─── static assets (CSS bundles, vendored JS like ApexCharts) ─────────────
# Mounted before the API router so `/static/*` is unambiguous. The SPA index
# stays on its own `@app.get('/')` handler below — this only serves /static/*.
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    print(f"[startup] /static/* mounted from {STATIC_DIR}", flush=True)


# ─── router mount ─────────────────────────────────────────────────────────
try:
    from api import router as _api_router
    app.include_router(_api_router)
    print("[startup] /api/* router mounted", flush=True)
except Exception as exc:
    print(f"[startup] api failed to load: {exc}", flush=True)


# ─── Health ──────────────────────────────────────────────────────────────────
@app.get("/api/health")
async def health() -> dict:
    return {
        "status": "shutting_down" if SHUTTING_DOWN else "ok",
        "pid": os.getpid(),
        "startup_id": STARTUP_ID,
        "scraper_active": bool(scraper_job.get("active")),
    }


@app.get("/api/status")
async def status_snapshot(auth=Depends(verify_token)) -> dict:
    """Full live snapshot of processes + tasks for the dashboard. The UI calls
    this on load/reconnect, then applies real-time 'status' socket.io deltas."""
    import status as _status
    snap = _status.registry.snapshot()
    snap["startup_id"] = STARTUP_ID
    snap["app_pid"] = os.getpid()
    return snap


# ─── Auth ────────────────────────────────────────────────────────────────────
@app.post("/api/auth/login")
async def login(request: Request) -> dict:
    body = await request.json()
    if body.get("password", "") == HARDCODED_PASSWORD:
        token = secrets.token_hex(32)
        TOKENS.add(token)
        return {"token": token, "success": True}
    raise HTTPException(status_code=401, detail="密碼錯誤")


@app.post("/api/auth/logout")
async def logout(credentials=Depends(security)) -> dict:
    if credentials is not None:
        TOKENS.discard(credentials.credentials)
    return {"success": True}


@app.get("/api/auth/check")
async def check_auth(auth=Depends(verify_token)) -> dict:
    return {"authenticated": True}


# ─── Schedules CRUD (lightweight wrapper over scheduler.py storage) ──────────
@app.get("/api/schedules")
async def list_schedules_api(auth=Depends(verify_token)) -> list[dict]:
    from scheduler import list_schedules
    return list_schedules()


@app.post("/api/schedules")
async def create_schedule_api(request: Request, auth=Depends(verify_token)) -> dict:
    from scheduler import create_schedule
    body = await request.json()
    return create_schedule(
        name=body["name"], cron=body["cron"],
        action=body["action"], args=body.get("args") or {},
        enabled=body.get("enabled", True),
    )


@app.patch("/api/schedules/{sched_id}")
async def patch_schedule_api(sched_id: str, request: Request, auth=Depends(verify_token)) -> dict:
    from scheduler import update_schedule
    return update_schedule(sched_id, await request.json())


@app.delete("/api/schedules/{sched_id}")
async def delete_schedule_api(sched_id: str, auth=Depends(verify_token)) -> dict:
    from scheduler import delete_schedule
    delete_schedule(sched_id)
    return {"deleted": sched_id}


@app.post("/api/schedules/{sched_id}/run")
async def run_schedule_now(sched_id: str, auth=Depends(verify_token)) -> dict:
    from scheduler import fire_schedule, get_schedule
    sched = get_schedule(sched_id)
    if sched is None:
        raise HTTPException(status_code=404, detail=f"schedule {sched_id} not found")
    status, reason = await fire_schedule(sched)
    return {"id": sched_id, "status": status, "reason": reason}


# ─── WebSocket progress stream ───────────────────────────────────────────────
@app.websocket("/ws/progress")
async def ws_progress(websocket: WebSocket, token: str = Query(None)):
    if not token or token not in TOKENS:
        await websocket.close(code=1008)
        return
    await progress_broadcast.connect(websocket)
    try:
        await websocket.send_json({
            "type": "lifecycle",
            "phase": "draining" if SHUTTING_DOWN else "ready",
            "startup_id": STARTUP_ID, "pid": os.getpid(),
        })
        if scraper_job["active"]:
            await websocket.send_json({
                "type": "scraper_start", "_resumed": True,
                "started_at": scraper_job["started_at"],
                "current_task": scraper_job["current_task"],
            })
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        progress_broadcast.disconnect(websocket)


# ─── Static SPA ──────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def spa_index() -> str:
    spa_path = STATIC_DIR / "index.html"
    if spa_path.exists():
        return spa_path.read_text(encoding="utf-8")
    return (
        "<!DOCTYPE html><html><head><title>馬場分析 / HorseLab</title></head>"
        "<body><h1>馬場分析 / HorseLab</h1>"
        "<p>v1 endpoints removed; </p>"
        "<p>API surface: <a href='/api/health'>/api/health</a> · "
        "<a href='/api/schema-info'>/api/schema-info</a> · "
        "<a href='/api/strategies'>/api/strategies</a></p>"
        "</body></html>"
    )


# ─── ASGI app (Socket.IO in front of FastAPI) ────────────────────────────────
# socketio.ASGIApp serves the Engine.IO/Socket.IO handshake on `/socket.io/*`
# and delegates everything else (REST, static, SPA, the legacy /ws/progress) to
# the FastAPI `app`. It forwards the ASGI `lifespan` scope to FastAPI, so the
# existing startup/shutdown logic still runs. Serve THIS object, not `app`
# (e.g. `uvicorn app:asgi`), or the /socket.io route won't exist.
asgi = socketio.ASGIApp(sio, other_asgi_app=app)


# ─── Entrypoint ──────────────────────────────────────────────────────────────
class GracefulServer(uvicorn.Server):
    """Broadcast a `draining` lifecycle event over /ws/progress before tearing
    down connections. Second signal escalates to a hard exit."""

    def handle_exit(self, sig, frame):
        global SHUTTING_DOWN
        if SHUTTING_DOWN:
            super().handle_exit(sig, frame)
            return
        SHUTTING_DOWN = True

        async def _drain_then_exit():
            try:
                await progress_broadcast.broadcast({
                    "type": "lifecycle", "phase": "draining",
                    "startup_id": STARTUP_ID, "pid": os.getpid(),
                })
                await asyncio.sleep(0.15)
            except Exception as exc:
                print(f"[shutdown] draining broadcast failed: {exc}", flush=True)
            super(GracefulServer, self).handle_exit(sig, frame)

        try:
            asyncio.get_event_loop().create_task(_drain_then_exit())
        except RuntimeError:
            super().handle_exit(sig, frame)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8005)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()
    config = uvicorn.Config(asgi, host=args.host, port=args.port)
    server = GracefulServer(config)
    server.run()
