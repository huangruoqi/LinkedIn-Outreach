"""Tests for dashboard routine configuration."""

from __future__ import annotations

import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from web import routines_config as rc  # noqa: E402


def test_validate_routine_rejects_bad_skill() -> None:
    err = rc.validate_routine(
        {"name": "X", "skill": "not-a-real-skill", "interval_minutes": 10, "active": True}
    )
    assert err is not None


def test_migrate_legacy_stage_routines() -> None:
    legacy = [
        {
            "id": "initial_connect",
            "name": "Initial Connect",
            "icon": "auto_fix_high",
            "stages": "cold,pending_connection",
            "prospect_count": 2,
            "status": "active",
        }
    ]
    migrated = rc._migrate_routines(legacy)
    assert len(migrated) == 2
    assert migrated[0]["skill"] == "sync-pending-connections"
    assert migrated[0]["interval_minutes"] == 30
    assert migrated[0]["active"] is True


def test_get_routines_display_includes_skill_fields() -> None:
    row = rc.to_display_routine(
        {
            "id": "t1",
            "name": "Sync",
            "skill": "sync-pending-connections",
            "interval_minutes": 30,
            "active": False,
        }
    )
    assert row["skill"] == "sync-pending-connections"
    assert row["interval_minutes"] == 30
    assert row["status"] == "disabled"


def test_upsert_routines_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base = tmp_path / "outreach"
    (base / "config").mkdir(parents=True)
    monkeypatch.setenv("OUTREACH_DATA_ROOT", str(base))
    monkeypatch.setenv("OUTREACH_MOCK", "0")

    data = rc.upsert_routines(
        [
            {
                "id": "t1",
                "name": "Sync",
                "skill": "sync-pending-connections",
                "interval_minutes": 15,
                "active": True,
            }
        ]
    )
    assert data["total"] == 1
    assert data["routines"][0]["skill"] == "sync-pending-connections"


def _valid_row(**overrides: object) -> dict[str, object]:
    base = {
        "id": "t1",
        "name": "Sync",
        "skill": "sync-pending-connections",
        "interval_minutes": 15,
        "active": True,
    }
    base.update(overrides)
    return base


def test_validate_routine_accepts_blank_window() -> None:
    assert rc.validate_routine(_valid_row()) is None
    assert (
        rc.validate_routine(
            _valid_row(active_window_start=None, active_window_end=None)
        )
        is None
    )
    assert (
        rc.validate_routine(_valid_row(active_window_start="", active_window_end=""))
        is None
    )


def test_validate_routine_accepts_full_window() -> None:
    assert (
        rc.validate_routine(
            _valid_row(active_window_start="09:00", active_window_end="17:30")
        )
        is None
    )
    assert (
        rc.validate_routine(
            _valid_row(active_window_start="22:00", active_window_end="06:00")
        )
        is None
    )


def test_validate_routine_rejects_partial_window() -> None:
    err = rc.validate_routine(
        _valid_row(active_window_start="09:00", active_window_end=None)
    )
    assert err is not None and "both" in err


def test_validate_routine_rejects_equal_window() -> None:
    err = rc.validate_routine(
        _valid_row(active_window_start="09:00", active_window_end="09:00")
    )
    assert err is not None and "differ" in err


def test_validate_routine_rejects_malformed_window() -> None:
    err = rc.validate_routine(
        _valid_row(active_window_start="9am", active_window_end="5pm")
    )
    assert err is not None and "HH:MM" in err
    err = rc.validate_routine(
        _valid_row(active_window_start="24:00", active_window_end="06:00")
    )
    assert err is not None


def test_in_active_window_no_restriction() -> None:
    row = {"active_window_start": None, "active_window_end": None}
    assert rc.in_active_window(row, now=datetime(2026, 5, 23, 3, 14)) is True


def test_in_active_window_same_day() -> None:
    row = {"active_window_start": "09:00", "active_window_end": "17:00"}
    assert rc.in_active_window(row, now=datetime(2026, 5, 23, 9, 0)) is True
    assert rc.in_active_window(row, now=datetime(2026, 5, 23, 12, 30)) is True
    assert rc.in_active_window(row, now=datetime(2026, 5, 23, 16, 59)) is True
    assert rc.in_active_window(row, now=datetime(2026, 5, 23, 17, 0)) is False
    assert rc.in_active_window(row, now=datetime(2026, 5, 23, 8, 59)) is False


def test_in_active_window_crosses_midnight() -> None:
    row = {"active_window_start": "22:00", "active_window_end": "06:00"}
    assert rc.in_active_window(row, now=datetime(2026, 5, 23, 22, 0)) is True
    assert rc.in_active_window(row, now=datetime(2026, 5, 23, 23, 59)) is True
    assert rc.in_active_window(row, now=datetime(2026, 5, 24, 0, 0)) is True
    assert rc.in_active_window(row, now=datetime(2026, 5, 24, 5, 59)) is True
    assert rc.in_active_window(row, now=datetime(2026, 5, 24, 6, 0)) is False
    assert rc.in_active_window(row, now=datetime(2026, 5, 24, 21, 59)) is False


def test_upsert_routines_persists_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "outreach"
    (base / "config").mkdir(parents=True)
    monkeypatch.setenv("OUTREACH_DATA_ROOT", str(base))
    monkeypatch.setenv("OUTREACH_MOCK", "0")

    data = rc.upsert_routines(
        [
            {
                "id": "t1",
                "name": "Sync",
                "skill": "sync-pending-connections",
                "interval_minutes": 15,
                "active": True,
                "active_window_start": "09:00",
                "active_window_end": "17:00",
            }
        ]
    )
    row = data["routines"][0]
    assert row["active_window_start"] == "09:00"
    assert row["active_window_end"] == "17:00"

    display = rc.get_routines_display()["routines"][0]
    assert display["active_window_label"] == "09:00\u201317:00"


def test_upsert_routines_rejects_bad_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "outreach"
    (base / "config").mkdir(parents=True)
    monkeypatch.setenv("OUTREACH_DATA_ROOT", str(base))
    monkeypatch.setenv("OUTREACH_MOCK", "0")

    with pytest.raises(ValueError):
        rc.upsert_routines(
            [
                {
                    "id": "t1",
                    "name": "Sync",
                    "skill": "sync-pending-connections",
                    "interval_minutes": 15,
                    "active": True,
                    "active_window_start": "09:00",
                    "active_window_end": None,
                }
            ]
        )


# ── Jitter / scheduling tests ────────────────────────────────────────────────


def _live_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable both mock-mode toggles so jitter is actually applied."""
    monkeypatch.setenv("OUTREACH_MOCK", "0")
    monkeypatch.delenv("ROUTINE_JITTER_DISABLED", raising=False)


def test_normalize_jitter_config_uses_defaults_for_garbage() -> None:
    cfg = rc._normalize_jitter_config(
        {"jitter_enabled": "yes", "jitter_min_minutes": "bad", "jitter_max_minutes": -5}
    )
    assert cfg["jitter_enabled"] is True
    # Bad min falls back to the default; negative max is clamped to 0 then
    # raised to ``min`` so we always have ``max >= min``.
    default_min = rc.DEFAULT_JITTER_CONFIG["jitter_min_minutes"]
    assert cfg["jitter_min_minutes"] == default_min
    assert cfg["jitter_max_minutes"] == default_min
    cfg2 = rc._normalize_jitter_config(
        {"jitter_min_minutes": 20, "jitter_max_minutes": 5}
    )
    # max < min should be clamped up to min, not silently swapped.
    assert cfg2["jitter_min_minutes"] == 20
    assert cfg2["jitter_max_minutes"] == 20


def test_compute_jitter_delta_zero_when_disabled_in_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _live_env(monkeypatch)
    cfg = {"jitter_enabled": False, "jitter_min_minutes": 5, "jitter_max_minutes": 15}
    for _ in range(20):
        assert rc.compute_jitter_delta_seconds(cfg) == 0


def test_compute_jitter_delta_zero_in_mock_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OUTREACH_MOCK", "1")
    monkeypatch.delenv("ROUTINE_JITTER_DISABLED", raising=False)
    cfg = dict(rc.DEFAULT_JITTER_CONFIG)
    for _ in range(20):
        assert rc.compute_jitter_delta_seconds(cfg) == 0


def test_compute_jitter_delta_zero_with_explicit_env_toggle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OUTREACH_MOCK", "0")
    monkeypatch.setenv("ROUTINE_JITTER_DISABLED", "1")
    cfg = dict(rc.DEFAULT_JITTER_CONFIG)
    for _ in range(20):
        assert rc.compute_jitter_delta_seconds(cfg) == 0


def test_compute_jitter_delta_within_configured_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _live_env(monkeypatch)
    cfg = {"jitter_enabled": True, "jitter_min_minutes": 5, "jitter_max_minutes": 15}
    min_sec = 5 * 60
    max_sec = 15 * 60
    seen_negative = False
    seen_positive = False
    rng = random.Random(0xC0FFEE)
    for _ in range(200):
        delta = rc.compute_jitter_delta_seconds(cfg, rng=rng)
        assert min_sec <= abs(delta) <= max_sec, delta
        if delta < 0:
            seen_negative = True
        if delta > 0:
            seen_positive = True
    assert seen_negative and seen_positive, "jitter should be symmetric over many draws"


def test_compute_jitter_delta_zero_when_max_is_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _live_env(monkeypatch)
    cfg = {"jitter_enabled": True, "jitter_min_minutes": 0, "jitter_max_minutes": 0}
    for _ in range(20):
        assert rc.compute_jitter_delta_seconds(cfg) == 0


def test_compute_next_run_at_no_jitter_in_mock_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OUTREACH_MOCK", "1")
    monkeypatch.delenv("ROUTINE_JITTER_DISABLED", raising=False)
    last = datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc)
    scheduled, jittered, delta = rc.compute_next_run_at(
        last, 30, dict(rc.DEFAULT_JITTER_CONFIG)
    )
    assert scheduled == last + timedelta(minutes=30)
    assert jittered == scheduled
    assert delta == 0


def test_compute_next_run_at_respects_rate_limit_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even with worst-case negative jitter, next run cannot collapse to now."""
    _live_env(monkeypatch)
    last = datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc)
    cfg = {"jitter_enabled": True, "jitter_min_minutes": 10, "jitter_max_minutes": 20}
    interval = 5  # interval < jitter_max => negative draw could go negative
    rng = random.Random(1)
    for _ in range(200):
        _, jittered, _ = rc.compute_next_run_at(last, interval, cfg, rng=rng)
        elapsed = (jittered - last).total_seconds()
        assert elapsed >= rc._MIN_EFFECTIVE_INTERVAL_SEC


def test_compute_next_run_at_within_jitter_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _live_env(monkeypatch)
    last = datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc)
    cfg = {"jitter_enabled": True, "jitter_min_minutes": 5, "jitter_max_minutes": 15}
    rng = random.Random(42)
    for _ in range(200):
        scheduled, jittered, delta = rc.compute_next_run_at(last, 30, cfg, rng=rng)
        assert scheduled == last + timedelta(minutes=30)
        assert 5 * 60 <= abs(delta) <= 15 * 60
        expected = scheduled + timedelta(seconds=delta)
        assert jittered == expected


def test_update_routine_after_run_persists_next_run_at(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "outreach"
    (base / "config").mkdir(parents=True)
    monkeypatch.setenv("OUTREACH_DATA_ROOT", str(base))
    _live_env(monkeypatch)

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
    rng = random.Random(7)
    decision = rc.update_routine_after_run(
        "t1", status="success", error=None, now=now, rng=rng
    )
    assert decision is not None

    stored = rc.load_config()["routines"][0]
    assert stored["last_run_at"] == now.isoformat()
    assert stored["next_run_at"] is not None
    next_run = datetime.fromisoformat(stored["next_run_at"])
    elapsed = (next_run - now).total_seconds()
    # 30 min interval +/- 5..15 min jitter, but never below the floor.
    assert (30 - 15) * 60 <= elapsed <= (30 + 15) * 60
    assert elapsed >= rc._MIN_EFFECTIVE_INTERVAL_SEC


def test_update_routine_after_run_deterministic_in_mock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "outreach"
    (base / "config").mkdir(parents=True)
    monkeypatch.setenv("OUTREACH_DATA_ROOT", str(base))
    monkeypatch.setenv("OUTREACH_MOCK", "1")
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
    decision = rc.update_routine_after_run(
        "t1", status="success", error=None, now=now
    )
    assert decision is not None
    assert decision["jitter_delta_seconds"] == 0
    assert decision["scheduled_at"] == decision["next_run_at"]
    expected = (now + timedelta(minutes=30)).isoformat()
    assert decision["next_run_at"] == expected


def test_load_config_seeds_default_jitter_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "outreach"
    (base / "config").mkdir(parents=True)
    monkeypatch.setenv("OUTREACH_DATA_ROOT", str(base))
    monkeypatch.setenv("OUTREACH_MOCK", "0")

    data = rc.load_config()
    assert data["routine_scheduling"] == rc.DEFAULT_JITTER_CONFIG


def test_set_jitter_config_roundtrip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "outreach"
    (base / "config").mkdir(parents=True)
    monkeypatch.setenv("OUTREACH_DATA_ROOT", str(base))
    monkeypatch.setenv("OUTREACH_MOCK", "0")

    cfg = rc.set_jitter_config(
        {"jitter_enabled": False, "jitter_min_minutes": 1, "jitter_max_minutes": 3}
    )
    assert cfg == {
        "jitter_enabled": False,
        "jitter_min_minutes": 1,
        "jitter_max_minutes": 3,
    }
    assert rc.get_jitter_config() == cfg
    # Round-tripped read preserves both sections.
    full = rc.load_config()
    assert full["routine_scheduling"] == cfg
    assert full["routines"]  # default seeded routines still present
