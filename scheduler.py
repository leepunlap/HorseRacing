"""
scheduler.py — Cron-like in-process scheduler for horseracing jobs.

Design
──────
Pure asyncio, no threads, no external dependency. One background task wakes
every minute on the wall-clock minute boundary and fires any schedule whose
cron expression matches `now`. Fired schedules call an action registered in
`_action_registry`, which is populated by app.py during startup. This keeps
the scheduler module decoupled from the rest of the app.

Persistence
───────────
Schedules live in `data/schedules.json` as a list of dicts. On every mutation
(create/update/delete, or after fire) we rewrite the file atomically (write
to .tmp then rename). Surviving restarts is the whole point — anything else
should be one-off via /api/jobs/*/start.

Cron syntax
───────────
Standard 5-field expression: "minute hour day-of-month month day-of-week".
Each field supports:
  - "*"            → any value
  - "5"            → exact value
  - "1,3,15"       → comma list
  - "1-5"          → inclusive range
  - "*/15"         → every Nth value, starting at field min
  - "10-20/2"      → range with step
Field ranges: minute 0-59, hour 0-23, dom 1-31, month 1-12, dow 0-6 (0=Sun, 7=Sun also).
Seconds are not supported (1-minute granularity is sufficient for racing jobs).

Skip-when-busy policy
─────────────────────
If an action raises "BusyError" or returns False, the schedule fires but is
marked `last_skip_reason="busy"` instead of `last_fire_status="ok"`. The
existing job machinery already 409s if a duplicate job is started, so we
catch that and record it without crashing the loop.
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from typing import Awaitable, Callable

BASE_DIR      = Path(__file__).parent
SCHEDULES_FILE = BASE_DIR / "data" / "schedules.json"

# Registry of (action_name → callable). app.py populates this at startup so
# the scheduler doesn't import job functions directly. Each callable receives
# the schedule's `args` dict and may raise to signal "skipped".
_action_registry: dict[str, Callable[[dict], object]] = {}

# Re-broadcast lifecycle/scheduler events on /ws/progress so the UI can react.
_broadcast: Callable[[dict], Awaitable[None]] | None = None


# ──────────────────────────────────────────────────────────────────────────
#  Cron expression parsing
# ──────────────────────────────────────────────────────────────────────────

_FIELD_BOUNDS = [
    (0, 59),   # minute
    (0, 23),   # hour
    (1, 31),   # day of month
    (1, 12),   # month
    (0, 6),    # day of week (0=Sun)
]


def _expand_field(token: str, lo: int, hi: int) -> set[int]:
    """Expand one cron field into the set of matching integers in [lo, hi]."""
    out: set[int] = set()
    for chunk in token.split(','):
        step = 1
        if '/' in chunk:
            chunk, step_s = chunk.split('/', 1)
            step = int(step_s)
            if step <= 0:
                raise ValueError(f"step must be > 0: {token!r}")
        if chunk == '*':
            a, b = lo, hi
        elif '-' in chunk:
            a_s, b_s = chunk.split('-', 1)
            a, b = int(a_s), int(b_s)
        else:
            a = b = int(chunk)
        # Normalize Sunday=7 to 0 for the dow field (lo=0,hi=6).
        if lo == 0 and hi == 6:
            if a == 7: a = 0
            if b == 7: b = 0
        if not (lo <= a <= hi and lo <= b <= hi and a <= b):
            raise ValueError(f"out-of-range in {token!r} (allowed {lo}-{hi})")
        out.update(range(a, b + 1, step))
    return out


def parse_cron(expr: str) -> tuple[set[int], set[int], set[int], set[int], set[int]]:
    """Parse a 5-field cron expression into (minutes, hours, doms, months, dows).

    Raises ValueError on malformed input — caught by the API layer to return 400.
    """
    parts = expr.strip().split()
    if len(parts) != 5:
        raise ValueError(f"cron must be 5 fields, got {len(parts)}: {expr!r}")
    return tuple(_expand_field(p, lo, hi) for p, (lo, hi) in zip(parts, _FIELD_BOUNDS))


def cron_matches(expr: str, dt: datetime) -> bool:
    """True if `dt` matches the cron expression at minute granularity.

    Standard cron semantics: if *both* dom and dow are restricted (neither
    is "*"), the schedule fires when *either* matches. If only one is
    restricted, only that one is checked.
    """
    mins, hours, doms, months, dows = parse_cron(expr)
    if dt.minute not in mins:  return False
    if dt.hour   not in hours: return False
    if dt.month  not in months: return False
    # dow: Python's weekday() is Mon=0..Sun=6; cron is Sun=0..Sat=6.
    py_dow = (dt.weekday() + 1) % 7
    # If either dom or dow is unrestricted, only the other matters.
    parts = expr.strip().split()
    dom_restricted = parts[2] != '*'
    dow_restricted = parts[4] != '*'
    dom_ok = dt.day in doms
    dow_ok = py_dow in dows
    if dom_restricted and dow_restricted:
        return dom_ok or dow_ok
    if dom_restricted:
        return dom_ok
    if dow_restricted:
        return dow_ok
    return True  # both *: any day


# ──────────────────────────────────────────────────────────────────────────
#  Storage
# ──────────────────────────────────────────────────────────────────────────

def _load_schedules() -> list[dict]:
    """Read schedules.json. Returns [] if missing or unreadable."""
    if not SCHEDULES_FILE.exists():
        return []
    try:
        with SCHEDULES_FILE.open() as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save_schedules(items: list[dict]) -> None:
    """Atomic write: tmp + rename so a crash mid-write doesn't corrupt the file."""
    SCHEDULES_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = SCHEDULES_FILE.with_suffix('.tmp')
    with tmp.open('w') as f:
        json.dump(items, f, indent=2, ensure_ascii=False)
    tmp.replace(SCHEDULES_FILE)


def list_schedules() -> list[dict]:
    return _load_schedules()


def get_schedule(sched_id: str) -> dict | None:
    for s in _load_schedules():
        if s.get('id') == sched_id:
            return s
    return None


def _validate_schedule(s: dict) -> None:
    """Raise ValueError if a schedule dict is missing required fields or has bad cron."""
    for k in ('name', 'cron', 'action'):
        if not s.get(k):
            raise ValueError(f"missing required field: {k}")
    if s['action'] not in _action_registry:
        raise ValueError(f"unknown action: {s['action']!r}; known: {list(_action_registry)}")
    parse_cron(s['cron'])   # raises if invalid


def create_schedule(name: str, cron: str, action: str, args: dict | None = None, enabled: bool = True) -> dict:
    sched = {
        'id':         secrets.token_hex(8),
        'name':       name.strip(),
        'cron':       cron.strip(),
        'action':     action,
        'args':       args or {},
        'enabled':    bool(enabled),
        'created_at': datetime.now().isoformat(timespec='seconds'),
        'last_fired_at':   None,
        'last_fire_status': None,   # 'ok' | 'error' | 'skipped'
        'last_skip_reason': None,
    }
    _validate_schedule(sched)
    items = _load_schedules()
    items.append(sched)
    _save_schedules(items)
    return sched


def update_schedule(sched_id: str, patch: dict) -> dict | None:
    items = _load_schedules()
    for i, s in enumerate(items):
        if s.get('id') == sched_id:
            for k in ('name', 'cron', 'action', 'args', 'enabled'):
                if k in patch:
                    s[k] = patch[k]
            _validate_schedule(s)
            items[i] = s
            _save_schedules(items)
            return s
    return None


def delete_schedule(sched_id: str) -> bool:
    items = _load_schedules()
    new   = [s for s in items if s.get('id') != sched_id]
    if len(new) == len(items):
        return False
    _save_schedules(new)
    return True


def _mark_fire(sched_id: str, status: str, reason: str | None = None) -> None:
    """Record fire-time metadata on a schedule. status ∈ {'ok','error','skipped'}."""
    items = _load_schedules()
    for s in items:
        if s.get('id') == sched_id:
            s['last_fired_at']     = datetime.now().isoformat(timespec='seconds')
            s['last_fire_status']  = status
            s['last_skip_reason']  = reason
            break
    _save_schedules(items)


# ──────────────────────────────────────────────────────────────────────────
#  Manual trigger (used by API and the loop)
# ──────────────────────────────────────────────────────────────────────────

async def fire_schedule(sched: dict) -> tuple[str, str | None]:
    """Invoke a schedule's action. Returns (status, reason).

    status: 'ok' if invoked, 'skipped' if action raised "busy", 'error' otherwise.
    Reason is a human-readable string when not 'ok'.
    """
    action_fn = _action_registry.get(sched['action'])
    if action_fn is None:
        return 'error', f"unknown action: {sched['action']}"
    try:
        action_fn(sched.get('args') or {})
        return 'ok', None
    except Exception as exc:
        msg = str(exc)
        # Distinguish "busy" from generic errors — start_* functions raise
        # HTTPException(409) when another job of the same kind is running.
        if 'already running' in msg.lower() or 'in progress' in msg.lower() or '409' in msg:
            return 'skipped', 'busy'
        return 'error', msg


# ──────────────────────────────────────────────────────────────────────────
#  Main loop
# ──────────────────────────────────────────────────────────────────────────

async def _broadcast_event(payload: dict) -> None:
    """Best-effort broadcast — swallow if no broadcaster wired or send fails."""
    if _broadcast is None:
        return
    try:
        await _broadcast(payload)
    except Exception:
        pass


async def run_scheduler_loop(broadcast_obj, is_shutting_down: Callable[[], bool]) -> None:
    """Wake every minute on the minute boundary, fire matching schedules.

    Cancelled by lifespan shutdown — the CancelledError propagates and we
    just return. No subprocesses are owned by this loop directly; we kick off
    jobs that own their own subprocesses.
    """
    global _broadcast
    _broadcast = broadcast_obj.broadcast if hasattr(broadcast_obj, 'broadcast') else None

    # Sleep to the next minute boundary so each tick lines up with cron.
    async def _sleep_to_next_minute():
        now = datetime.now()
        # +1s slack so we don't fire the same minute twice if we wake a tick early.
        next_min = (now.replace(second=0, microsecond=0) + timedelta(minutes=1, seconds=1))
        delay = (next_min - now).total_seconds()
        await asyncio.sleep(max(delay, 1))

    try:
        while not is_shutting_down():
            await _sleep_to_next_minute()
            if is_shutting_down():
                break
            now = datetime.now().replace(second=0, microsecond=0)
            for sched in _load_schedules():
                if not sched.get('enabled', True):
                    continue
                try:
                    if not cron_matches(sched['cron'], now):
                        continue
                except ValueError:
                    # Bad cron string — skip silently; the API rejects bad
                    # strings on create, so we only get here if the JSON
                    # was hand-edited to something invalid.
                    continue
                status, reason = await fire_schedule(sched)
                _mark_fire(sched['id'], status, reason)
                await _broadcast_event({
                    'type':   'schedule_fired',
                    'id':     sched['id'],
                    'name':   sched['name'],
                    'action': sched['action'],
                    'status': status,
                    'reason': reason,
                    'at':     now.isoformat(timespec='seconds'),
                })
    except asyncio.CancelledError:
        # Normal path on shutdown.
        raise
