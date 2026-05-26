"""Tests for the background routine scheduler.

Focused on the jitter-aware due check and the run-log fields. We don't spin
up the asyncio loop here; the bits worth testing are the deterministic helpers
(``_is_due``, ``_scheduled_at_for``) plus integration with
``routines_config.append_run_log`` / ``update_routine_after_run``.
"""

from __future__ import annotations

import json
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from web import routine_scheduler as rs  # noqa: E402
from web import routines_config as rc  # noqa: E402


def _routine(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": "t1",
        "name": "Sync",
        "skill": "sync-pending-connections",
        "interval_minutes": 30,
        "active": True,
        "active_window_start": None,
        "active_window_end": None,
        "last_run_at": None,
        "last_status": None,
        "last_error": None,
        "next_run_at": None,
    }
    base.update(overrides)
    return base


def test_is_due_runs_when_brand_new() -> None:
    now = datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc)
    assert rs._is_due(_routine(), now=now) is True


def test_is_due_false_when_inactive() -> None:
    now = datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc)
    assert rs._is_due(_routine(active=False), now=now) is False


def test_is_due_uses_next_run_at_when_set() -> None:
    last = datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc)
    next_run = last + timedelta(minutes=35)
    row = _routine(last_run_at=last.isoformat(), next_run_at=next_run.isoformat())
    # Before next_run_at: not due even if interval has elapsed.
    too_early = last + timedelta(minutes=30, seconds=1)
    assert rs._is_due(row, now=too_early) is False
    # At/after next_run_at: due.
    assert rs._is_due(row, now=next_run) is True
    assert rs._is_due(row, now=next_run + timedelta(minutes=1)) is True


def test_is_due_falls_back_to_interval_when_next_run_at_missing() -> None:
    """Pre-jitter rows (migrated) keep the legacy ``interval`` behavior."""
    last = datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc)
    row = _routine(last_run_at=last.isoformat(), interval_minutes=30)
    assert rs._is_due(row, now=last + timedelta(minutes=29)) is False
    assert rs._is_due(row, now=last + timedelta(minutes=30)) is True


def test_is_due_respects_active_window_even_if_jittered_time_passed() -> None:
    """Jitter that pushes ``next_run_at`` outside the active window must not
    cause the routine to fire outside that window."""
    last = datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc)
    next_run = last + timedelta(minutes=20)
    row = _routine(
        active_window_start="09:00",
        active_window_end="17:00",
        last_run_at=last.isoformat(),
        next_run_at=next_run.isoformat(),
    )
    # next_run_at has passed, but we're outside the active window: not due.
    outside = datetime(2026, 5, 25, 20, 0)  # naive => local time
    assert rs._is_due(row, now=outside) is False
    # Inside the window and past next_run_at: due.
    inside = datetime(2026, 5, 25, 14, 0)
    assert rs._is_due(row, now=inside) is True


def test_scheduled_at_for_returns_unjittered_time() -> None:
    last = datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc)
    row = _routine(last_run_at=last.isoformat(), interval_minutes=30)
    expected = (last + timedelta(minutes=30)).isoformat()
    assert rs._scheduled_at_for(row) == expected


def test_scheduled_at_for_none_when_no_last_run() -> None:
    assert rs._scheduled_at_for(_routine()) is None


def test_append_run_log_includes_jitter_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "outreach"
    (base / "config").mkdir(parents=True)
    (base / "logs").mkdir(parents=True)
    monkeypatch.setenv("OUTREACH_DATA_ROOT", str(base))
    monkeypatch.setenv("OUTREACH_MOCK", "0")

    rc.append_run_log(
        routine_id="t1",
        skill="sync-pending-connections",
        status="success",
        started_at="2026-05-25T12:07:00+00:00",
        finished_at="2026-05-25T12:07:10+00:00",
        scheduled_at="2026-05-25T12:00:00+00:00",
        jittered_at="2026-05-25T12:07:00+00:00",
        jitter_delta_seconds=420,
    )

    log_path = base / "logs" / rc.RUNS_LOG
    row = json.loads(log_path.read_text(encoding="utf-8").strip())
    assert row["scheduled_at"] == "2026-05-25T12:00:00+00:00"
    assert row["jittered_at"] == "2026-05-25T12:07:00+00:00"
    assert row["jitter_delta_seconds"] == 420


def test_full_cycle_logs_original_and_jittered_times(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: after a recorded run, the next scheduled time differs from
    the next jittered time by exactly ``jitter_delta_seconds`` and falls inside
    the configured jitter envelope."""
    base = tmp_path / "outreach"
    (base / "config").mkdir(parents=True)
    monkeypatch.setenv("OUTREACH_DATA_ROOT", str(base))
    monkeypatch.setenv("OUTREACH_MOCK", "0")
    monkeypatch.delenv("ROUTINE_JITTER_DISABLED", raising=False)

    rc.upsert_routines(
        [
            {
                "id": "t1",
                "name": "Sync",
                "skill": "sync-pending-connections",
                "interval_minutes": 30,
                "active": True,
            }
        ]
    )
    now = datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc)
    rng = random.Random(99)
    decision = rc.update_routine_after_run(
        "t1", status="success", error=None, now=now, rng=rng
    )
    assert decision is not None

    scheduled = datetime.fromisoformat(decision["scheduled_at"])
    next_run = datetime.fromisoformat(decision["next_run_at"])
    delta = decision["jitter_delta_seconds"]

    assert scheduled == now + timedelta(minutes=30)
    assert next_run == scheduled + timedelta(seconds=delta)
    # Default jitter is ±5..15 minutes.
    assert 5 * 60 <= abs(delta) <= 15 * 60
