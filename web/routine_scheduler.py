"""
Background asyncio scheduler: runs configured Claude skills on interval.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from web import routines_config
from web.skill_runner import run_named_skill

logger = logging.getLogger("web.routine_scheduler")

TICK_SECONDS = 30
_running_locks: dict[str, asyncio.Lock] = {}


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _is_due(routine: dict[str, Any], *, now: datetime | None = None) -> bool:
    """True if ``routine`` should fire at ``now``.

    Two gates are enforced:

    1. ``in_active_window`` - jitter can push the planned fire time outside
       the configured business-hours window; we never actually run outside
       it. The routine simply waits until the next window opens.
    2. ``now >= next_run_at`` when the routine has been scheduled before.
       ``next_run_at`` is the *jittered* time computed at the end of the
       previous run. On a fresh routine (no ``next_run_at`` and no
       ``last_run_at``) we fire immediately; if only ``last_run_at`` is set
       (e.g. migrated from a pre-jitter config) we fall back to the legacy
       ``interval_minutes`` check so behavior never regresses.

    The active window is interpreted in *server local time* (matching the
    historical scheduler), while ``next_run_at`` / ``last_run_at`` are
    persisted as UTC ISO timestamps. When ``now`` is omitted the two clocks
    are read independently; when ``now`` is supplied (tests) a naive value is
    treated as both local-time-for-window and UTC-for-interval.
    """
    if not routine.get("active"):
        return False
    if now is None:
        if not routines_config.in_active_window(routine):
            return False
        now_utc = datetime.now(timezone.utc)
    else:
        if not routines_config.in_active_window(routine, now=now):
            return False
        now_utc = (
            now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now
        )
    next_run = _parse_iso(routine.get("next_run_at"))
    if next_run is not None:
        return now_utc >= next_run
    last = _parse_iso(routine.get("last_run_at"))
    if last is None:
        return True
    interval_min = int(routine.get("interval_minutes") or 60)
    return (now_utc - last).total_seconds() >= interval_min * 60


# Backwards compat alias for any callers/tests using the prior private name.
_due = _is_due


def _scheduled_at_for(routine: dict[str, Any]) -> str | None:
    """Original (unjittered) scheduled time = ``last_run_at + interval``."""
    last = _parse_iso(routine.get("last_run_at"))
    if last is None:
        return None
    interval_min = int(routine.get("interval_minutes") or 60)
    return (last + timedelta(minutes=interval_min)).isoformat()


async def _run_one(routine: dict[str, Any]) -> None:
    rid = routine["id"]
    skill = routine["skill"]
    lock = _running_locks.setdefault(rid, asyncio.Lock())
    if lock.locked():
        logger.info("routine %s already running, skip", rid)
        return
    async with lock:
        scheduled_at = _scheduled_at_for(routine)
        jittered_at = routine.get("next_run_at")
        jitter_delta_seconds: int | None = None
        if scheduled_at and jittered_at:
            sch = _parse_iso(scheduled_at)
            jit = _parse_iso(jittered_at)
            if sch and jit:
                jitter_delta_seconds = int((jit - sch).total_seconds())
        started_dt = datetime.now(timezone.utc)
        started = started_dt.isoformat()
        logger.info(
            "routine %s starting skill=%s scheduled_at=%s jittered_at=%s "
            "jitter_delta_s=%s started_at=%s",
            rid,
            skill,
            scheduled_at,
            jittered_at,
            jitter_delta_seconds,
            started,
        )
        result = await asyncio.to_thread(run_named_skill, skill)
        finished = datetime.now(timezone.utc).isoformat()
        status = "success" if result.ok else "failed"
        routines_config.append_run_log(
            routine_id=rid,
            skill=skill,
            status=status,
            scheduled_at=scheduled_at,
            jittered_at=jittered_at,
            jitter_delta_seconds=jitter_delta_seconds,
            started_at=started,
            finished_at=finished,
            error=result.error,
            stdout_tail=result.stdout,
        )
        decision = routines_config.update_routine_after_run(
            rid, status=status, error=result.error
        )
        if decision is not None:
            logger.info(
                "routine %s scheduled next run scheduled_at=%s next_run_at=%s "
                "jitter_delta_s=%s",
                rid,
                decision.get("scheduled_at"),
                decision.get("next_run_at"),
                decision.get("jitter_delta_seconds"),
            )
        if result.ok:
            logger.info("routine %s finished ok", rid)
        else:
            logger.warning("routine %s failed: %s", rid, result.error)


async def _tick() -> None:
    data = routines_config.load_config()
    for routine in data.get("routines") or []:
        if _is_due(routine):
            asyncio.create_task(_run_one(routine))


async def scheduler_loop(stop: asyncio.Event) -> None:
    logger.info("routine scheduler started (tick=%ss)", TICK_SECONDS)
    while not stop.is_set():
        try:
            await _tick()
        except Exception:
            logger.exception("routine scheduler tick failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=TICK_SECONDS)
        except asyncio.TimeoutError:
            pass
    logger.info("routine scheduler stopped")
