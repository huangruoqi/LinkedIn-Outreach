"""
Update checker / installer for the LinkedIn-Outreach project.

Provides:
  * Library functions used by the MCP server and tests:
      - get_installed_version()
      - get_latest_version()
      - check_for_updates()
      - apply_update()
  * A small CLI used by the Makefile:
      $ python tools/updater.py check
      $ python tools/updater.py update [--yes] [--dry-run] [--branch BRANCH]

Design goals
------------
1. **Read-only by default.**  ``check`` never touches the working tree.
2. **Non-destructive update.**  ``update`` uses ``git fetch`` + ``git merge --ff-only``
   so a divergent local branch is refused with a clear error.  Local files that
   are gitignored (``.env``, ``outreach/config/persona.json``, ``outreach/logs/*``,
   ``outreach/prospects/*.json``, ``outreach/conversations/*.json``,
   ``outreach/connections.json`` …) are never touched by ``git pull``, so the
   operator's persona, prospect data, and audit logs are preserved.
3. **Explicit + logged.**  Every action records a JSON line to
   ``logs/updater.log`` and an audit entry to ``outreach/logs/actions.jsonl``
   (when the directory exists).
4. **Recoverable.**  Before pulling, the current commit SHA is captured and
   printed (and logged) so the user can roll back with a single
   ``git reset --hard <sha>`` if something goes wrong post-update.

The module deliberately uses only the Python standard library so it can run
before ``uv sync`` succeeds.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
_LOG_DIR = _ROOT / "logs"
_UPDATER_LOG = _LOG_DIR / "updater.log"
_ACTION_LOG = _ROOT / "outreach" / "logs" / "actions.jsonl"

# GitHub repo coordinates. Override via env if a fork is used.
DEFAULT_OWNER = "huangruoqi"
DEFAULT_REPO = "LinkedIn-Outreach"
DEFAULT_BRANCH = "main"

_USER_AGENT = "linkedin-outreach-updater/1.0"

logger = logging.getLogger("linkedin.updater")


# ── Logging helpers ───────────────────────────────────────────────────────────


def _ensure_logger() -> None:
    """Configure file + stderr logging on first use."""
    if logger.handlers:
        return
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    fh = logging.FileHandler(_UPDATER_LOG, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    logger.setLevel(logging.INFO)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _append_action_log(entry: dict[str, Any]) -> None:
    """Best-effort append to outreach/logs/actions.jsonl."""
    try:
        _ACTION_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _ACTION_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        # Never let audit-log failure break the update flow.
        logger.warning("could not append to %s", _ACTION_LOG)


# ── Git helpers ───────────────────────────────────────────────────────────────


def _run_git(
    *args: str,
    cwd: Path | None = None,
    check: bool = True,
    capture: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run ``git <args>`` and return the CompletedProcess.

    ``check=True`` raises ``subprocess.CalledProcessError`` on non-zero exit.
    """
    cmd = ["git", *args]
    return subprocess.run(
        cmd,
        cwd=str(cwd or _ROOT),
        check=check,
        capture_output=capture,
        text=True,
    )


def _is_git_repo(repo_root: Path | None = None) -> bool:
    root = repo_root or _ROOT
    if not (root / ".git").exists():
        return False
    try:
        _run_git("rev-parse", "--is-inside-work-tree", cwd=root)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def _current_branch(repo_root: Path | None = None) -> str | None:
    try:
        out = _run_git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo_root)
        branch = out.stdout.strip()
        return branch or None
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def _current_sha(repo_root: Path | None = None) -> str | None:
    try:
        out = _run_git("rev-parse", "HEAD", cwd=repo_root)
        sha = out.stdout.strip()
        return sha or None
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def _short_sha(sha: str | None) -> str | None:
    if not sha:
        return None
    return sha[:7]


def _working_tree_dirty(repo_root: Path | None = None) -> bool:
    """Return True if tracked files are modified (ignored files don't count)."""
    try:
        out = _run_git("status", "--porcelain", "--untracked-files=no", cwd=repo_root)
        return bool(out.stdout.strip())
    except (subprocess.CalledProcessError, FileNotFoundError):
        # Be conservative: if we cannot check, treat as dirty.
        return True


def _remote_url(remote: str = "origin", repo_root: Path | None = None) -> str | None:
    try:
        out = _run_git("remote", "get-url", remote, cwd=repo_root)
        url = out.stdout.strip()
        return url or None
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def _parse_github_slug(url: str | None) -> tuple[str, str] | None:
    """Return (owner, repo) parsed from a GitHub URL, or None."""
    if not url:
        return None
    m = re.search(
        r"github\.com[:/]+([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+?)(?:\.git)?/?$",
        url,
    )
    if not m:
        return None
    return m.group(1), m.group(2)


# ── Project version ───────────────────────────────────────────────────────────


def _read_pyproject_version() -> str | None:
    """Extract ``version`` from ``pyproject.toml`` without requiring tomllib."""
    path = _ROOT / "pyproject.toml"
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    m = re.search(r'^\s*version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return m.group(1) if m else None


@dataclass
class InstalledVersion:
    """Snapshot of what is currently installed in this repo."""

    project_version: str | None
    git_sha: str | None
    git_short_sha: str | None
    git_branch: str | None
    repo_root: str
    remote_url: str | None
    is_git_repo: bool
    skills_installed: list[str] = field(default_factory=list)
    checked_at: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RemoteVersion:
    """Snapshot of the latest version available on GitHub."""

    owner: str
    repo: str
    branch: str
    latest_sha: str | None
    latest_short_sha: str | None
    latest_committed_at: str | None
    latest_message: str | None
    api_url: str
    checked_at: str = field(default_factory=_now_iso)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class UpdateStatus:
    """Result of comparing local vs remote."""

    installed: InstalledVersion
    remote: RemoteVersion
    update_available: bool
    behind_by: int | None  # number of commits behind, or None if unknown
    rollback_sha: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "installed": self.installed.to_dict(),
            "remote": self.remote.to_dict(),
            "update_available": self.update_available,
            "behind_by": self.behind_by,
            "rollback_sha": self.rollback_sha,
        }


# ── Skill discovery ───────────────────────────────────────────────────────────


def _list_skills(skill_src: Path | None = None) -> list[str]:
    src = skill_src or (_ROOT / "outreach" / "skills")
    if not src.is_dir():
        return []
    names: list[str] = []
    for p in sorted(src.iterdir()):
        if p.is_dir() and (p / "SKILL.md").is_file():
            names.append(p.name)
    return names


# ── Public read-only API ──────────────────────────────────────────────────────


def get_installed_version() -> InstalledVersion:
    """Inspect the local checkout and return version metadata."""
    is_repo = _is_git_repo()
    sha = _current_sha() if is_repo else None
    branch = _current_branch() if is_repo else None
    remote_url = _remote_url() if is_repo else None
    return InstalledVersion(
        project_version=_read_pyproject_version(),
        git_sha=sha,
        git_short_sha=_short_sha(sha),
        git_branch=branch,
        repo_root=str(_ROOT),
        remote_url=remote_url,
        is_git_repo=is_repo,
        skills_installed=_list_skills(),
    )


def _github_owner_repo(override_owner: str | None, override_repo: str | None) -> tuple[str, str]:
    """Resolve the GitHub owner/repo, preferring overrides → env → git remote → defaults."""
    if override_owner and override_repo:
        return override_owner, override_repo

    env_slug = os.environ.get("LINKEDIN_OUTREACH_REPO_SLUG")
    if env_slug and "/" in env_slug:
        owner, repo = env_slug.split("/", 1)
        return owner.strip(), repo.strip()

    parsed = _parse_github_slug(_remote_url())
    if parsed:
        return parsed

    return DEFAULT_OWNER, DEFAULT_REPO


def _http_get_json(url: str, timeout: float = 8.0) -> Any:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": _USER_AGENT,
        },
    )
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec - GitHub API
        body = resp.read().decode("utf-8", errors="replace")
    return json.loads(body)


def get_latest_version(
    owner: str | None = None,
    repo: str | None = None,
    branch: str | None = None,
) -> RemoteVersion:
    """Query GitHub for the latest commit on the configured branch.

    Network failures (offline, GitHub API rate limit, etc.) are captured in
    ``RemoteVersion.error`` rather than raised.
    """
    o, r = _github_owner_repo(owner, repo)
    b = branch or os.environ.get("LINKEDIN_OUTREACH_BRANCH") or DEFAULT_BRANCH
    api_url = f"https://api.github.com/repos/{o}/{r}/commits/{b}"

    try:
        data = _http_get_json(api_url)
    except urllib.error.HTTPError as exc:
        return RemoteVersion(
            owner=o,
            repo=r,
            branch=b,
            latest_sha=None,
            latest_short_sha=None,
            latest_committed_at=None,
            latest_message=None,
            api_url=api_url,
            error=f"HTTP {exc.code} from GitHub API: {exc.reason}",
        )
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return RemoteVersion(
            owner=o,
            repo=r,
            branch=b,
            latest_sha=None,
            latest_short_sha=None,
            latest_committed_at=None,
            latest_message=None,
            api_url=api_url,
            error=f"network error: {exc}",
        )

    sha = (data or {}).get("sha") if isinstance(data, dict) else None
    commit = (data or {}).get("commit") if isinstance(data, dict) else None
    committer = (commit or {}).get("committer") if isinstance(commit, dict) else None
    when = (committer or {}).get("date") if isinstance(committer, dict) else None
    msg_full = (commit or {}).get("message") if isinstance(commit, dict) else None
    msg = (msg_full.splitlines()[0] if isinstance(msg_full, str) and msg_full else None)
    return RemoteVersion(
        owner=o,
        repo=r,
        branch=b,
        latest_sha=sha,
        latest_short_sha=_short_sha(sha),
        latest_committed_at=when,
        latest_message=msg,
        api_url=api_url,
    )


def _count_commits_between(local_sha: str, remote_sha: str) -> int | None:
    """How many commits ``local`` is behind ``remote`` (ancestor → descendant)."""
    if not local_sha or not remote_sha or local_sha == remote_sha:
        return 0
    try:
        out = _run_git(
            "rev-list",
            "--count",
            f"{local_sha}..{remote_sha}",
            check=False,
        )
    except FileNotFoundError:
        return None
    if out.returncode != 0:
        return None
    try:
        return int(out.stdout.strip())
    except ValueError:
        return None


def check_for_updates(
    owner: str | None = None,
    repo: str | None = None,
    branch: str | None = None,
) -> UpdateStatus:
    """Return whether a remote update is available without modifying anything."""
    installed = get_installed_version()
    remote = get_latest_version(owner=owner, repo=repo, branch=branch)

    update_available = False
    behind: int | None = None
    if remote.latest_sha and installed.git_sha:
        if remote.latest_sha == installed.git_sha:
            behind = 0
        else:
            update_available = True
            behind = _count_commits_between(installed.git_sha, remote.latest_sha)
    elif remote.latest_sha and not installed.git_sha:
        update_available = True

    return UpdateStatus(
        installed=installed,
        remote=remote,
        update_available=update_available,
        behind_by=behind,
        rollback_sha=installed.git_sha,
    )


# ── Update apply ──────────────────────────────────────────────────────────────


@dataclass
class UpdateResult:
    """Outcome of an ``apply_update`` invocation."""

    ok: bool
    dry_run: bool
    before_sha: str | None
    after_sha: str | None
    branch: str | None
    skills_synced: list[str]
    deps_synced: bool
    message: str
    rollback_command: str | None
    steps: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _record_step(
    steps: list[dict[str, Any]],
    name: str,
    ok: bool,
    detail: str | None = None,
) -> None:
    steps.append(
        {
            "step": name,
            "ok": ok,
            "detail": detail,
            "at": _now_iso(),
        }
    )


def _sync_skills_to_home(
    skill_src: Path,
    user_skills_dir: Path,
) -> list[str]:
    """Mirror project skills into the user's Claude skills dir (rsync semantics)."""
    if not skill_src.is_dir():
        return []
    user_skills_dir.mkdir(parents=True, exist_ok=True)
    synced: list[str] = []
    for d in sorted(skill_src.iterdir()):
        if not d.is_dir() or not (d / "SKILL.md").is_file():
            continue
        dest = user_skills_dir / d.name
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(d, dest)
        synced.append(d.name)
    return synced


def _maybe_run_uv_sync() -> tuple[bool, str | None]:
    """Run ``uv sync`` if ``uv`` is available. Returns (ran, error)."""
    if not shutil.which("uv"):
        return False, "uv binary not on PATH; skipping dependency sync"
    try:
        cp = subprocess.run(
            ["uv", "sync"],
            cwd=str(_ROOT),
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        return False, f"uv sync failed to launch: {exc}"
    if cp.returncode != 0:
        return False, cp.stderr.strip() or cp.stdout.strip() or "uv sync exited non-zero"
    return True, None


def apply_update(
    branch: str | None = None,
    dry_run: bool = False,
    sync_skills_home: bool | None = None,
    user_skills_dir: Path | None = None,
    run_uv_sync: bool = True,
) -> UpdateResult:
    """
    Apply available updates safely.

    Steps (each fully logged):
      1. Validate this is a git checkout with a clean working tree (tracked files).
      2. Capture the current SHA for rollback.
      3. ``git fetch`` the configured remote/branch.
      4. ``git merge --ff-only`` — refuses to rewrite local history.
      5. Re-sync gitignored-safe artifacts: Python deps via ``uv sync`` and
         Claude skills into the user's skill dir (when not LOCAL).
      6. Emit an audit log entry.

    ``dry_run=True`` performs the safety checks but skips fetch / merge / sync.

    Local config (``.env``, ``outreach/config/persona.json``,
    ``outreach/logs/*``, ``outreach/prospects/*.json``,
    ``outreach/conversations/*.json``, ``outreach/connections.json``) is
    gitignored and therefore untouched by ``git pull``.
    """
    _ensure_logger()
    steps: list[dict[str, Any]] = []
    before_sha = _current_sha()
    rollback_cmd = (
        f"git -C {_ROOT} reset --hard {before_sha}" if before_sha else None
    )
    branch_name = (
        branch
        or os.environ.get("LINKEDIN_OUTREACH_BRANCH")
        or _current_branch()
        or DEFAULT_BRANCH
    )

    def fail(message: str) -> UpdateResult:
        logger.error("apply_update failed: %s", message)
        _append_action_log(
            {
                "action": "self_update",
                "timestamp": _now_iso(),
                "ok": False,
                "dry_run": dry_run,
                "before_sha": before_sha,
                "branch": branch_name,
                "error": message,
            }
        )
        return UpdateResult(
            ok=False,
            dry_run=dry_run,
            before_sha=before_sha,
            after_sha=before_sha,
            branch=branch_name,
            skills_synced=[],
            deps_synced=False,
            message=message,
            rollback_command=rollback_cmd,
            steps=steps,
        )

    # 1. Git repo + cleanliness.
    if not _is_git_repo():
        _record_step(steps, "git_repo_check", False, "not a git checkout")
        return fail(
            "Not a git checkout — re-clone with ./install.sh or "
            "git clone https://github.com/huangruoqi/LinkedIn-Outreach.git"
        )
    _record_step(steps, "git_repo_check", True)

    if _working_tree_dirty():
        _record_step(
            steps,
            "working_tree_clean",
            False,
            "tracked files are modified",
        )
        return fail(
            "Working tree has local modifications to tracked files. "
            "Commit, stash, or revert them and re-run. "
            "Local config (.env, persona.json, logs, prospects) is gitignored "
            "and never affected by this check."
        )
    _record_step(steps, "working_tree_clean", True)

    if dry_run:
        logger.info("apply_update: dry-run (no fetch/merge performed)")
        _record_step(steps, "dry_run", True, "skipping fetch + merge")
        _append_action_log(
            {
                "action": "self_update",
                "timestamp": _now_iso(),
                "ok": True,
                "dry_run": True,
                "before_sha": before_sha,
                "branch": branch_name,
            }
        )
        return UpdateResult(
            ok=True,
            dry_run=True,
            before_sha=before_sha,
            after_sha=before_sha,
            branch=branch_name,
            skills_synced=[],
            deps_synced=False,
            message=(
                "Dry-run OK. Working tree is clean; "
                "git fetch + git merge --ff-only would now run."
            ),
            rollback_command=rollback_cmd,
            steps=steps,
        )

    logger.info(
        "apply_update: starting (branch=%s before=%s)",
        branch_name,
        _short_sha(before_sha),
    )

    # 2. git fetch
    try:
        _run_git("fetch", "origin", branch_name)
    except subprocess.CalledProcessError as exc:
        _record_step(steps, "git_fetch", False, exc.stderr or str(exc))
        return fail(
            f"git fetch origin {branch_name} failed: "
            f"{(exc.stderr or exc.stdout or '').strip() or exc}"
        )
    _record_step(steps, "git_fetch", True, f"origin/{branch_name}")

    # 3. git merge --ff-only
    try:
        _run_git("merge", "--ff-only", f"origin/{branch_name}")
    except subprocess.CalledProcessError as exc:
        _record_step(steps, "git_merge_ff_only", False, exc.stderr or str(exc))
        return fail(
            "git merge --ff-only refused to update. Your branch has commits "
            "that are not on origin/"
            + branch_name
            + ". Rebase or reset manually, e.g.: "
            + f"git -C {_ROOT} fetch origin && git -C {_ROOT} reset --hard origin/{branch_name}"
        )
    _record_step(steps, "git_merge_ff_only", True, f"origin/{branch_name}")

    after_sha = _current_sha()
    logger.info("apply_update: now at %s", _short_sha(after_sha))

    # 4. uv sync (best-effort).
    deps_synced = False
    if run_uv_sync:
        ran, err = _maybe_run_uv_sync()
        deps_synced = ran
        _record_step(
            steps,
            "uv_sync",
            ran if err is None else False,
            err or "uv sync ok",
        )
        if err and not ran:
            logger.warning("apply_update: %s", err)

    # 5. Re-sync skills to ~/.claude/skills unless LOCAL mode is requested.
    if sync_skills_home is None:
        sync_skills_home = (
            os.environ.get("LINKEDIN_OUTREACH_INSTALL_LOCAL", "0") != "1"
            and os.environ.get("LINKEDIN_OUTREACH_SYNC_SKILLS_HOME", "1") == "1"
        )
    synced: list[str] = []
    if sync_skills_home:
        target = user_skills_dir or Path(
            os.environ.get("USER_CLAUDE_SKILLS")
            or (Path.home() / ".claude" / "skills")
        )
        try:
            synced = _sync_skills_to_home(_ROOT / "outreach" / "skills", target)
            _record_step(
                steps,
                "skills_sync",
                True,
                f"{len(synced)} → {target}",
            )
        except OSError as exc:
            _record_step(steps, "skills_sync", False, str(exc))
            logger.warning("apply_update: skill sync failed: %s", exc)
    else:
        _record_step(steps, "skills_sync", True, "skipped (LOCAL mode)")

    _append_action_log(
        {
            "action": "self_update",
            "timestamp": _now_iso(),
            "ok": True,
            "dry_run": False,
            "before_sha": before_sha,
            "after_sha": after_sha,
            "branch": branch_name,
            "skills_synced": synced,
            "deps_synced": deps_synced,
            "rollback_command": rollback_cmd,
        }
    )

    if before_sha == after_sha:
        message = "Already up to date."
    else:
        message = (
            f"Updated {_short_sha(before_sha)} → {_short_sha(after_sha)} "
            f"on {branch_name}."
        )

    return UpdateResult(
        ok=True,
        dry_run=False,
        before_sha=before_sha,
        after_sha=after_sha,
        branch=branch_name,
        skills_synced=synced,
        deps_synced=deps_synced,
        message=message,
        rollback_command=rollback_cmd,
        steps=steps,
    )


# ── CLI ───────────────────────────────────────────────────────────────────────


def _print_check(status: UpdateStatus, as_json: bool) -> int:
    if as_json:
        print(json.dumps(status.to_dict(), indent=2, ensure_ascii=False))
        if status.remote.error:
            return 2
        return 1 if status.update_available else 0

    inst = status.installed
    rem = status.remote
    print("LinkedIn-Outreach update check")
    print(f"  repo:        {inst.repo_root}")
    print(f"  remote URL:  {inst.remote_url or '(none)'}")
    print(f"  branch:      {inst.git_branch or '(detached)'}")
    print(
        f"  installed:   {inst.git_short_sha or '(unknown)'}  "
        f"(pyproject {inst.project_version or '?'})"
    )
    if rem.error:
        print(f"  latest:      <error: {rem.error}>")
        print("  → could not reach GitHub. Set GITHUB_TOKEN to raise the rate limit,")
        print("    or check your network and try again with: make check-update")
        return 2

    print(
        f"  latest:      {rem.latest_short_sha} on {rem.owner}/{rem.repo}@{rem.branch}"
    )
    if rem.latest_committed_at:
        print(f"  committed:   {rem.latest_committed_at}")
    if rem.latest_message:
        print(f"  message:     {rem.latest_message}")

    if status.update_available:
        if status.behind_by is not None and status.behind_by > 0:
            print(f"\n  ⬆ Update available — you are {status.behind_by} commit(s) behind.")
        else:
            print("\n  ⬆ Update available.")
        print("  Run:   make update")
        return 1

    print("\n  ✓ Up to date.")
    return 0


def _print_update(result: UpdateResult, as_json: bool) -> int:
    if as_json:
        print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
        return 0 if result.ok else 1

    if result.ok:
        print(f"[update] {result.message}")
        if result.dry_run:
            print("[update] (dry-run — no changes made)")
        else:
            if result.deps_synced:
                print("[update]   ✓ uv sync")
            if result.skills_synced:
                print(
                    f"[update]   ✓ synced {len(result.skills_synced)} skill(s) "
                    "→ ~/.claude/skills"
                )
        if result.rollback_command:
            print(f"[update] rollback if needed:  {result.rollback_command}")
        return 0

    print(f"[update] FAILED: {result.message}", file=sys.stderr)
    if result.rollback_command:
        print(
            f"[update] rollback to prior version:  {result.rollback_command}",
            file=sys.stderr,
        )
    return 1


def main(argv: list[str] | None = None) -> int:
    _ensure_logger()
    parser = argparse.ArgumentParser(
        prog="updater",
        description="Check for or apply updates to the LinkedIn-Outreach repo.",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON output.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_check = sub.add_parser("check", help="Check whether a remote update is available.")
    p_check.add_argument("--branch", default=None)
    p_check.add_argument("--owner", default=None)
    p_check.add_argument("--repo", default=None)

    p_update = sub.add_parser("update", help="Apply available updates safely.")
    p_update.add_argument("--branch", default=None)
    p_update.add_argument("--dry-run", action="store_true")
    p_update.add_argument(
        "--no-uv-sync",
        action="store_true",
        help="Skip 'uv sync' after pulling.",
    )
    p_update.add_argument(
        "--local",
        action="store_true",
        help="Do not sync skills to ~/.claude/skills (project-only mode).",
    )
    p_update.add_argument(
        "--yes",
        action="store_true",
        help="Skip interactive confirmation.",
    )

    args = parser.parse_args(argv)

    if args.cmd == "check":
        status = check_for_updates(
            owner=args.owner, repo=args.repo, branch=args.branch
        )
        return _print_check(status, as_json=args.json)

    if args.cmd == "update":
        status = check_for_updates(branch=args.branch)
        if not status.update_available and not args.dry_run:
            if args.json:
                payload = {
                    "ok": True,
                    "dry_run": False,
                    "message": "Already up to date.",
                    "installed": status.installed.to_dict(),
                    "remote": status.remote.to_dict(),
                }
                print(json.dumps(payload, indent=2, ensure_ascii=False))
            else:
                inst = status.installed
                rem = status.remote
                print(
                    f"[update] Already up to date "
                    f"(at {inst.git_short_sha or '?'} on {rem.branch})."
                )
                if rem.error:
                    print(f"[update] (note: {rem.error})")
            return 0

        if not args.yes and not args.dry_run and sys.stdin.isatty():
            inst = status.installed
            rem = status.remote
            print(
                f"About to fast-forward {inst.git_short_sha or '?'} "
                f"→ {rem.latest_short_sha or '?'} on {rem.branch}."
            )
            print("Local .env, persona.json, logs, and prospect data are untouched.")
            try:
                answer = input("Proceed? [y/N] ").strip().lower()
            except EOFError:
                answer = ""
            if answer not in ("y", "yes"):
                print("[update] aborted by user")
                return 0

        result = apply_update(
            branch=args.branch,
            dry_run=args.dry_run,
            sync_skills_home=False if args.local else None,
            run_uv_sync=not args.no_uv_sync,
        )
        return _print_update(result, as_json=args.json)

    parser.error("unknown command")
    return 2  # pragma: no cover


if __name__ == "__main__":
    sys.exit(main())
