"""
Local regression harness: models the real operator pipeline order:

1. **LinkedIn connection** — Harness calls MCP ``send_connection_request`` (mock-backed) plus
   ``upsert_prospect`` / ``save_connection`` with ``pending`` after ``handle_load_test_case``
   (same as prod / operator pipeline).
2. **sync-pending-connections** — ``claude -p`` runs the **sync-pending-connections** skill;
   the harness then calls ``promote_pending_connections_from_mock`` so ``connections.json``
   matches mock ``is_first_degree_connection`` even if the subprocess did not invoke MCP.
3. **conversation-planner rounds** — ``claude -p`` runs **conversation-planner** in **batch
   mode** (no ``prospect_id``); the skill discovers candidates via MCP ``get_connections``.
   The harness still applies ``send_*`` from the parsed PlannedMessage and snapshots
   ``upsert_conversation`` from the mock thread (plan-only inside ``claude -p`` avoids
   double delivery).

``tools/server.py`` is used in-process so paths follow ``_outreach_base()`` (e.g.
``outreach/mock/`` in mock mode).

See: ``docs/designs/outreach-workflow-regression-tests-design.md``
"""

from __future__ import annotations

import asyncio
import json
import logging
from math import exp
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("outreach.regression")

REPO_ROOT = Path(__file__).resolve().parent.parent
_TOOLS = REPO_ROOT / "tools"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

import mock as _mock  # noqa: E402  — tools/mock.py

FIXTURES = REPO_ROOT / "tests" / "fixtures" / "conversation-planner"
PROSPECT_FIXTURE = FIXTURES / "prospect_alex.json"

# Installed skill ids (see outreach/skills/*/SKILL.md frontmatter ``name``).
CONVERSATION_PLANNER_SKILL = "conversation-planner"
SYNC_PENDING_CONNECTIONS_SKILL = "sync-pending-connections"

# Canonical profile URL for mock sessions (matches prospect_alex.json).
REGRESSION_PROFILE_URL = "https://www.linkedin.com/in/alex-chen-softeng/"
PROSPECT_ID = "alex_chen_softeng"

_SERVER_MODULE: Any = None


def get_server_module() -> Any:
    """Load ``tools/server.py`` once (MCP tool implementations + outreach paths)."""
    global _SERVER_MODULE
    if _SERVER_MODULE is not None:
        return _SERVER_MODULE
    import importlib.util

    path = REPO_ROOT / "tools" / "server.py"
    spec = importlib.util.spec_from_file_location("linkedin_mcp_server_regression", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load server spec from {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _SERVER_MODULE = mod
    return mod


def extract_json_object(raw: str) -> dict[str, Any]:
    """Return the first JSON object embedded in *raw* (handles markdown fences, prose)."""
    cleaned = re.sub(r"```(?:json)?\s*", "", raw)
    cleaned = cleaned.strip()
    depth = 0
    start: int | None = None
    for i, ch in enumerate(cleaned):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                candidate = cleaned[start : i + 1]
                try:
                    obj = json.loads(candidate)
                    if isinstance(obj, dict):
                        return obj
                except json.JSONDecodeError:
                    continue
    raise ValueError(f"No valid JSON object found in response:\n{raw[:500]}")


def parse_planned_message(stdout: str) -> dict[str, Any]:
    """Parse a :class:`PlannedMessage`-shaped dict from model stdout."""
    obj = extract_json_object(stdout)
    if not obj.get("action"):
        raise ValueError("PlannedMessage missing required field: action")
    return obj


def _utc_ts(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (
        dt.astimezone(timezone.utc)
        .replace(microsecond=0)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


def _normalize_attachments(raw: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not isinstance(raw, list):
        return out
    for a in raw:
        if not isinstance(a, dict):
            continue
        t = a.get("type")
        if t not in ("resume", "image", "link", "document"):
            continue
        item: dict[str, Any] = {"type": t}
        if "url" in a:
            item["url"] = a.get("url")
        if "filename" in a:
            item["filename"] = a.get("filename")
        out.append(item)
    return out


async def reset_regression_artifacts(mod: Any, profile_url: str, prospect_id: str) -> None:
    """Clear mock session state and on-disk mock outreach rows for a clean scenario run."""
    key = _mock.normalise_url(profile_url)
    _mock.sessions.pop(key, None)
    _mock.clear_persisted_mock_session(profile_url)
    base: Path = mod._outreach_base()
    mod._atomic_write_json(base / "connections.json", {"connections": []})
    conv_path = base / "conversations" / f"{prospect_id}.json"
    if conv_path.is_file():
        conv_path.unlink()
    prospect_path = base / "prospects" / f"{prospect_id}.json"
    if prospect_path.is_file():
        prospect_path.unlink()


def claude_cli_available() -> bool:
    return shutil.which("claude") is not None


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        logger.warning("regression: could not read %s", path)
        return None


async def _connection_row(mod: Any, profile_url: str) -> dict[str, Any] | None:
    raw = await mod.get_connections()
    data = _load_json(REPO_ROOT / "outreach" / "mock" / "connections.json")
    for row in data.get("connections") or []:
        if isinstance(row, dict) and row.get("profile_url") == profile_url:
            return row
    return None


async def assert_state_after_linkedin_connect(
    mod: Any, profile_url: str, prospect_name: str
) -> None:
    import pytest

    row = await _connection_row(mod, profile_url)
    if row is None:
        pytest.fail("post-connect: missing connections.json row")
    if row.get("connection_status") != "pending":
        pytest.fail(
            f"post-connect: expected pending got {row.get('connection_status')!r}"
        )
    if prospect_name and prospect_name not in (row.get("name") or ""):
        pytest.fail(
            f"post-connect: name mismatch row={row.get('name')!r} "
            f"expected substring {prospect_name!r}"
        )


async def assert_state_after_sync_pending(
    mod: Any,
    case_id: str,
    profile_url: str,
    connection_accepted: bool,
) -> None:
    import pytest

    row = await _connection_row(mod, profile_url)
    if row is None:
        pytest.fail("post-sync: missing connection row")
    st = row.get("connection_status")
    if connection_accepted:
        if st != "connected":
            pytest.fail(
                f"post-sync: expected connected for case {case_id!r}, got {st!r}"
            )
    else:
        if st != "pending":
            pytest.fail(
                f"post-sync: expected pending for non-accept case {case_id!r}, got {st!r}"
            )


async def assert_state_after_planner_round(
    mod: Any,
    profile_url: str,
    prospect_id: str,
    allowed_stages: set[str],
    session: _mock.MockSession,
) -> None:
    import pytest

    raw = await mod.get_conversation(prospect_id)
    if isinstance(raw, str) and raw.startswith("error:"):
        pytest.fail(f"get_conversation failed: {raw}")
    conv = json.loads(raw)
    if conv.get("outreach_stage") not in allowed_stages:
        pytest.fail(
            f"post-planner: allowed_stages={allowed_stages!r} got {conv.get('outreach_stage')!r}"
        )


def invoke_claude_cli(prompt: str) -> str:
    """
    Run ``claude -p`` from repo root with default tools so **MCP** (and skills) work.

    Permission mode defaults to ``bypassPermissions`` so non-interactive regression
    can call MCP tools; override with ``REGRESSION_CLAUDE_PERMISSION_MODE``.

    Model defaults to Haiku 4.5 (cheaper, separate quota from Sonnet/Opus).
    Set ``CLAUDE_MODEL`` (same as ``outreach/planner.py``) or pass ``--model haiku``
    aliases supported by the Claude CLI.
    """
    timeout = int(os.environ.get("REGRESSION_CLAUDE_TIMEOUT_SEC", "600"))
    perm = os.environ.get("REGRESSION_CLAUDE_PERMISSION_MODE", "bypassPermissions").strip()
    model = os.environ.get("CLAUDE_MODEL", "haiku").strip()
    cmd = [
        "claude",
        "-p",
        prompt,
        "--model",
        model,
        "--permission-mode",
        perm,
    ]
    env = os.environ.copy()
    home = env.get("HOME", "")
    env["PATH"] = f"{home}/.local/bin:{home}/.cargo/bin:{env.get('PATH', '')}"

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "claude executable not found (PATH). Install Claude Code CLI."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"claude subprocess exceeded timeout={timeout}s") from exc
    out = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
    if proc.returncode != 0:
        raise RuntimeError(
            f"claude exited {proc.returncode}. stdout+stderr tail:\n{out[-4000:]}"
        )
    return proc.stdout or ""

# Transition specs: allowed_actions are sets; optional checks keep models from drifting too far.
RoundSpec = dict[str, Any]

REGRESSION_SPECS: dict[str, dict[str, Any]] = {
    "happy_path": {
        "rounds": [
            {
                "id": "hp_r0_step1",
                "allowed_actions": frozenset({"send_followup_message"}),
                "allowed_stages": frozenset({"engaged"}),
            },
            {
                "id": "hp_r1_step2",
                "allowed_actions": frozenset({"send_followup_message"}),
                "allowed_stages": frozenset({"engaged"}),
            },
            {
                "id": "hp_r1_step3",
                "allowed_actions": frozenset({"send_followup_message"}),
                "allowed_stages": frozenset({"engaged"}),
            },
            {
                "id": "hp_r1_step4",
                "allowed_actions": frozenset({"send_followup_message"}),
                "allowed_stages": frozenset({"engaged"}),
            },
            {
                "id": "hp_r2_step5",
                "allowed_actions": frozenset({"send_followup_message"}),
                "allowed_stages": frozenset({"engaged", "ended"}),
            },
            {
                "id": "hp_r2_step6",
                "allowed_actions": frozenset({"send_followup_message"}),
                "allowed_stages": frozenset({"ended"}),
            },
        ],
        "repeat_final": True,
    },
}


def assert_transition(
    spec: RoundSpec,
    plan: dict[str, Any],
    *,
    round_index: int,
) -> None:
    import pytest

    rid = spec["id"]
    action = plan.get("action")
    allowed = spec["allowed_actions"]
    if action not in allowed:
        pytest.fail(
            f"transition[{rid}] round={round_index}: action={action!r} "
            f"not in allowed={sorted(allowed)}"
        )
    stages = spec.get("allowed_stages")
    if stages is not None:
        stage = plan.get("stage")
        if stage not in stages:
            pytest.fail(
                f"transition[{rid}] round={round_index}: stage={stage!r} "
                f"not in allowed={sorted(stages)}"
            )


def _prospect_has_resume_in_history(session: _mock.MockSession) -> bool:
    for e in session.history:
        if e.get("self"):
            continue
        for a in e.get("attachments") or []:
            if isinstance(a, dict) and a.get("type") == "resume":
                return True
    return False


def scenario_terminal_satisfied(case_id: str, session: _mock.MockSession, plan: dict[str, Any]) -> bool:
    if plan.get("end_conversation") or plan.get("action") in ("mark_ended", "mark_dead"):
        return True
    if case_id in ("happy_path", "eager_referral") and _prospect_has_resume_in_history(session):
        return True
    if case_id == "not_interested":
        if plan.get("ended_reason") == "not_interested":
            return True
        for e in session.history:
            if not e.get("self") and "not looking" in (e.get("message") or "").lower():
                if plan.get("action") in ("mark_dead", "mark_ended"):
                    return True
    if case_id == "no_reply" and plan.get("ended_reason") in (
        "no_response",
        "no_response_timeout",
    ):
        return True
    if case_id == "ghosted_cold" and plan.get("action") in ("mark_dead", "mark_ended"):
        return True
    return False


async def run_scenario_async(case_id: str) -> None:
    """
    End-to-end sequence for one ``TEST_CASES`` entry:

    1. LinkedIn invite: ``send_connection_request`` + ``upsert_prospect`` + ``save_connection`` (pending).
    2. sync-pending-connections: ``get_connections`` → ``is_first_degree_connection`` → ``save_connection`` (connected when mock accepts).
    3. Persist ``upsert_conversation`` from mock thread, then conversation-planner rounds (``claude -p`` + MCP deliveries).
    """
    import pytest

    if case_id not in _mock.TEST_CASES:
        pytest.fail(f"unknown TEST_CASE case_id={case_id!r}")
    if case_id not in REGRESSION_SPECS:
        pytest.fail(f"no REGRESSION_SPECS for case_id={case_id!r}")

    mod = get_server_module()
    url = REGRESSION_PROFILE_URL
    prospect_id = PROSPECT_ID
    tc = _mock.TEST_CASES[case_id]
    connection_accepted = bool(tc.get("connection_accepted"))
    prospect_name = str((tc.get("prospect") or {}).get("name") or "Alex")

    await reset_regression_artifacts(mod, url, prospect_id)
    invoke_claude_cli(f"Connect to {REGRESSION_PROFILE_URL}")
    await assert_state_after_linkedin_connect(mod, url, prospect_name)

    try:
        invoke_claude_cli("Run sync-pending-connections skill")
    except Exception as exc:
        pytest.fail(f"sync-pending-connections skill (claude -p): {exc}")

    await assert_state_after_sync_pending(mod, case_id, url, connection_accepted)

    meta = REGRESSION_SPECS[case_id]
    rounds_spec: list[RoundSpec] = meta["rounds"]
    repeat_final: bool = meta["repeat_final"]

    for round_index in range(len(rounds_spec)):
        if not repeat_final and round_index >= len(rounds_spec):
            pytest.fail(
                f"{case_id}: exhausted spec rounds ({len(rounds_spec)}) at loop {round_index}"
            )
        spec_idx = round_index if round_index < len(rounds_spec) else len(rounds_spec) - 1
        spec = rounds_spec[spec_idx]

        try:
            invoke_claude_cli("Run conversation-planner skill")
        except Exception as exc:
            pytest.fail(f"[{spec['id']}] round={round_index} invoke_claude_cli: {exc}")
        session = _mock.get_session(url)
        if session is None:
            pytest.fail(
                f"[{spec['id']}] round={round_index}: no mock session for {url!r}"
            )
        allowed_stages = spec.get("allowed_stages")
        await assert_state_after_planner_round(mod, url, prospect_id, allowed_stages, session)


def run_scenario(case_id: str) -> None:
    """Sync wrapper for :func:`run_scenario_async`."""
    asyncio.run(run_scenario_async(case_id))
