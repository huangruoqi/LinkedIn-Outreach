"""Tests for tools/updater.py."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_TOOLS = Path(__file__).resolve().parents[1] / "tools"
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

import updater as up  # noqa: E402


# ── Helpers ───────────────────────────────────────────────────────────────────


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.setdefault("GIT_AUTHOR_NAME", "Test")
    env.setdefault("GIT_AUTHOR_EMAIL", "test@example.com")
    env.setdefault("GIT_COMMITTER_NAME", "Test")
    env.setdefault("GIT_COMMITTER_EMAIL", "test@example.com")
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )


def _make_fake_repo(
    tmp_path: Path,
    *,
    name: str = "LinkedIn-Outreach",
    project_version: str = "0.1.0",
    extra_skills: tuple[str, ...] = ("conversation-planner", "send-connection-request"),
) -> Path:
    """Materialise a stand-in repo with the layout updater.py expects."""
    repo = tmp_path / name
    (repo / "tools").mkdir(parents=True)
    (repo / "outreach" / "skills").mkdir(parents=True)
    (repo / "outreach" / "logs").mkdir(parents=True)
    (repo / "logs").mkdir(parents=True)

    (repo / "pyproject.toml").write_text(
        textwrap.dedent(
            f"""
            [project]
            name = "linkedin-outreach"
            version = "{project_version}"
            requires-python = ">=3.10"
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    (repo / "tools" / "server.py").write_text("# stub\n", encoding="utf-8")
    for skill in extra_skills:
        d = repo / "outreach" / "skills" / skill
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(f"# {skill}\n", encoding="utf-8")
    # A non-skill directory under skills/ should be ignored.
    (repo / "outreach" / "skills" / "_not_a_skill").mkdir(parents=True)

    _git("init", "--initial-branch=main", cwd=repo)
    _git("add", "-A", cwd=repo)
    _git("commit", "-m", "initial", cwd=repo)
    return repo


@pytest.fixture()
def fake_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = _make_fake_repo(tmp_path)
    monkeypatch.setattr(up, "_ROOT", repo)
    monkeypatch.setattr(up, "_LOG_DIR", repo / "logs")
    monkeypatch.setattr(up, "_UPDATER_LOG", repo / "logs" / "updater.log")
    monkeypatch.setattr(
        up, "_ACTION_LOG", repo / "outreach" / "logs" / "actions.jsonl"
    )
    # Reset the cached logger handlers between tests so log files land in the
    # current tmpdir, not whichever path the previous test seeded.
    up.logger.handlers.clear()
    return repo


# ── Slug + version parsing ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://github.com/huangruoqi/LinkedIn-Outreach.git", ("huangruoqi", "LinkedIn-Outreach")),
        ("https://github.com/huangruoqi/LinkedIn-Outreach", ("huangruoqi", "LinkedIn-Outreach")),
        ("git@github.com:huangruoqi/LinkedIn-Outreach.git", ("huangruoqi", "LinkedIn-Outreach")),
        ("ssh://git@github.com/some-org/repo-name.git", ("some-org", "repo-name")),
        ("https://gitlab.com/huangruoqi/LinkedIn-Outreach.git", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_github_slug(url, expected):
    assert up._parse_github_slug(url) == expected


def test_read_pyproject_version(fake_repo: Path):
    assert up._read_pyproject_version() == "0.1.0"


def test_read_pyproject_version_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(up, "_ROOT", tmp_path)
    assert up._read_pyproject_version() is None


def test_list_skills(fake_repo: Path):
    skills = up._list_skills(fake_repo / "outreach" / "skills")
    assert skills == ["conversation-planner", "send-connection-request"]


def test_github_owner_repo_env_overrides_remote(monkeypatch):
    monkeypatch.setenv("LINKEDIN_OUTREACH_REPO_SLUG", "acme/repo")
    monkeypatch.setattr(up, "_remote_url", lambda *_a, **_k: "https://github.com/other/x.git")
    assert up._github_owner_repo(None, None) == ("acme", "repo")


def test_github_owner_repo_remote_when_no_env(monkeypatch):
    monkeypatch.delenv("LINKEDIN_OUTREACH_REPO_SLUG", raising=False)
    monkeypatch.setattr(
        up, "_remote_url", lambda *_a, **_k: "git@github.com:fork-owner/repo.git"
    )
    assert up._github_owner_repo(None, None) == ("fork-owner", "repo")


def test_github_owner_repo_default_when_nothing(monkeypatch):
    monkeypatch.delenv("LINKEDIN_OUTREACH_REPO_SLUG", raising=False)
    monkeypatch.setattr(up, "_remote_url", lambda *_a, **_k: None)
    owner, repo = up._github_owner_repo(None, None)
    assert (owner, repo) == (up.DEFAULT_OWNER, up.DEFAULT_REPO)


# ── Installed version ─────────────────────────────────────────────────────────


def test_get_installed_version(fake_repo: Path):
    info = up.get_installed_version()
    assert info.is_git_repo is True
    assert info.project_version == "0.1.0"
    assert info.git_branch == "main"
    assert info.git_sha and len(info.git_sha) == 40
    assert info.git_short_sha == info.git_sha[:7]
    assert info.repo_root == str(fake_repo)
    assert "conversation-planner" in info.skills_installed


def test_get_installed_version_no_git(tmp_path, monkeypatch):
    monkeypatch.setattr(up, "_ROOT", tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="x"\nversion="9.9.9"\n', encoding="utf-8"
    )
    info = up.get_installed_version()
    assert info.is_git_repo is False
    assert info.project_version == "9.9.9"
    assert info.git_sha is None
    assert info.skills_installed == []


# ── Remote version + comparison ───────────────────────────────────────────────


def test_check_for_updates_up_to_date(fake_repo: Path, monkeypatch):
    installed_sha = up._current_sha()
    fake_remote = up.RemoteVersion(
        owner="huangruoqi",
        repo="LinkedIn-Outreach",
        branch="main",
        latest_sha=installed_sha,
        latest_short_sha=installed_sha[:7],
        latest_committed_at="2030-01-01T00:00:00Z",
        latest_message="initial",
        api_url="https://api.github.com/repos/x/y/commits/main",
    )
    monkeypatch.setattr(up, "get_latest_version", lambda **_: fake_remote)

    status = up.check_for_updates()
    assert status.update_available is False
    assert status.behind_by == 0
    assert status.rollback_sha == installed_sha


def test_check_for_updates_when_behind(fake_repo: Path, monkeypatch):
    before_sha = up._current_sha()
    # Add a second commit so we can identify "behind by 1" relative to the first.
    (fake_repo / "extra.txt").write_text("more\n", encoding="utf-8")
    _git("add", "-A", cwd=fake_repo)
    _git("commit", "-m", "second", cwd=fake_repo)
    after_sha = up._current_sha()
    # Reset working tree back to the first commit so HEAD is the "old" version.
    _git("reset", "--hard", before_sha, cwd=fake_repo)

    fake_remote = up.RemoteVersion(
        owner="huangruoqi",
        repo="LinkedIn-Outreach",
        branch="main",
        latest_sha=after_sha,
        latest_short_sha=after_sha[:7],
        latest_committed_at="2030-01-02T00:00:00Z",
        latest_message="second",
        api_url="https://api.github.com/repos/x/y/commits/main",
    )
    monkeypatch.setattr(up, "get_latest_version", lambda **_: fake_remote)

    status = up.check_for_updates()
    assert status.update_available is True
    assert status.behind_by == 1
    assert status.rollback_sha == before_sha


def test_check_for_updates_propagates_remote_error(fake_repo: Path, monkeypatch):
    err_remote = up.RemoteVersion(
        owner="huangruoqi",
        repo="LinkedIn-Outreach",
        branch="main",
        latest_sha=None,
        latest_short_sha=None,
        latest_committed_at=None,
        latest_message=None,
        api_url="https://api.github.com/repos/x/y/commits/main",
        error="network error: offline",
    )
    monkeypatch.setattr(up, "get_latest_version", lambda **_: err_remote)
    status = up.check_for_updates()
    assert status.update_available is False
    assert status.remote.error == "network error: offline"


# ── apply_update safety ───────────────────────────────────────────────────────


def test_apply_update_refuses_when_not_git_repo(tmp_path, monkeypatch):
    monkeypatch.setattr(up, "_ROOT", tmp_path)
    monkeypatch.setattr(up, "_LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(up, "_UPDATER_LOG", tmp_path / "logs" / "updater.log")
    monkeypatch.setattr(up, "_ACTION_LOG", tmp_path / "actions.jsonl")
    up.logger.handlers.clear()

    result = up.apply_update(dry_run=False)
    assert result.ok is False
    assert "Not a git checkout" in result.message
    assert result.rollback_command is None


def test_apply_update_refuses_when_tree_dirty(fake_repo: Path):
    (fake_repo / "tools" / "server.py").write_text("# tampered\n", encoding="utf-8")
    result = up.apply_update(dry_run=False)
    assert result.ok is False
    assert "Working tree has local modifications" in result.message
    assert result.rollback_command and result.rollback_command.startswith(
        f"git -C {fake_repo} reset --hard "
    )


def test_apply_update_dry_run_clean_tree(fake_repo: Path):
    result = up.apply_update(dry_run=True)
    assert result.ok is True
    assert result.dry_run is True
    assert result.before_sha == result.after_sha == up._current_sha()
    assert "Dry-run OK" in result.message
    steps = [s["step"] for s in result.steps]
    assert "git_repo_check" in steps
    assert "working_tree_clean" in steps
    assert "dry_run" in steps


def test_apply_update_ignores_gitignored_local_files(
    fake_repo: Path, monkeypatch
):
    """Gitignored local files must survive a clean-tree check.

    The update flow intentionally *appends* one entry to
    ``outreach/logs/actions.jsonl`` (this is the "explicit + logged" success
    criterion) but it must never truncate the file or touch other gitignored
    artifacts such as ``persona.json``.
    """
    (fake_repo / ".gitignore").write_text(
        "outreach/config/persona.json\noutreach/logs/actions.jsonl\n",
        encoding="utf-8",
    )
    _git("add", ".gitignore", cwd=fake_repo)
    _git("commit", "-m", "add gitignore", cwd=fake_repo)

    (fake_repo / "outreach" / "config").mkdir(parents=True, exist_ok=True)
    persona = fake_repo / "outreach" / "config" / "persona.json"
    persona_payload = '{"persona": {"name": "Operator"}}'
    persona.write_text(persona_payload, encoding="utf-8")
    actions = fake_repo / "outreach" / "logs" / "actions.jsonl"
    actions.write_text('{"action":"noop"}\n', encoding="utf-8")

    # Clean check must still pass even with gitignored files present.
    assert up._working_tree_dirty() is False

    result = up.apply_update(dry_run=True)
    assert result.ok is True

    # Persona is fully untouched.
    assert persona.read_text(encoding="utf-8") == persona_payload

    # actions.jsonl: prior line preserved, new self_update audit entry appended.
    lines = actions.read_text(encoding="utf-8").splitlines()
    assert lines[0] == '{"action":"noop"}'
    appended = [json.loads(line) for line in lines[1:]]
    assert any(
        e.get("action") == "self_update" and e.get("dry_run") is True
        for e in appended
    )


def test_apply_update_fast_forwards_from_local_remote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """End-to-end: real git fast-forward against a local 'origin' remote."""
    upstream = _make_fake_repo(tmp_path, name="Upstream")
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", str(upstream), str(clone)],
        check=True,
        capture_output=True,
        text=True,
    )
    # Add a commit to upstream.
    (upstream / "NEW.txt").write_text("hello\n", encoding="utf-8")
    _git("add", "-A", cwd=upstream)
    _git("commit", "-m", "added NEW.txt", cwd=upstream)
    new_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(upstream),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    monkeypatch.setattr(up, "_ROOT", clone)
    monkeypatch.setattr(up, "_LOG_DIR", clone / "logs")
    monkeypatch.setattr(up, "_UPDATER_LOG", clone / "logs" / "updater.log")
    monkeypatch.setattr(
        up, "_ACTION_LOG", clone / "outreach" / "logs" / "actions.jsonl"
    )
    up.logger.handlers.clear()

    user_skills = tmp_path / "user_claude_skills"
    result = up.apply_update(
        dry_run=False,
        sync_skills_home=True,
        user_skills_dir=user_skills,
        run_uv_sync=False,
    )

    assert result.ok is True, result.message
    assert result.after_sha == new_sha
    assert result.before_sha != result.after_sha
    assert (clone / "NEW.txt").exists()
    assert set(result.skills_synced) >= {"conversation-planner", "send-connection-request"}
    for s in result.skills_synced:
        assert (user_skills / s / "SKILL.md").is_file()

    # Audit log line written for the successful update.
    action_log = clone / "outreach" / "logs" / "actions.jsonl"
    assert action_log.is_file()
    entries = [json.loads(line) for line in action_log.read_text("utf-8").splitlines()]
    assert any(
        e.get("action") == "self_update" and e.get("ok") is True for e in entries
    )


def test_apply_update_refuses_divergent_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A clone that has diverged from origin must not be force-updated."""
    upstream = _make_fake_repo(tmp_path, name="Upstream2")
    clone = tmp_path / "clone2"
    subprocess.run(
        ["git", "clone", str(upstream), str(clone)],
        check=True,
        capture_output=True,
        text=True,
    )

    # Upstream advances by one commit.
    (upstream / "U.txt").write_text("u\n", encoding="utf-8")
    _git("add", "-A", cwd=upstream)
    _git("commit", "-m", "upstream commit", cwd=upstream)

    # Clone advances independently (committed) — fast-forward must refuse.
    (clone / "L.txt").write_text("l\n", encoding="utf-8")
    _git("add", "-A", cwd=clone)
    _git("commit", "-m", "local divergent commit", cwd=clone)

    monkeypatch.setattr(up, "_ROOT", clone)
    monkeypatch.setattr(up, "_LOG_DIR", clone / "logs")
    monkeypatch.setattr(up, "_UPDATER_LOG", clone / "logs" / "updater.log")
    monkeypatch.setattr(
        up, "_ACTION_LOG", clone / "outreach" / "logs" / "actions.jsonl"
    )
    up.logger.handlers.clear()

    result = up.apply_update(dry_run=False, run_uv_sync=False, sync_skills_home=False)
    assert result.ok is False
    assert "fast-forward" in result.message or "merge --ff-only" in result.message
    # Local file from the divergent commit is still there — no destructive op.
    assert (clone / "L.txt").exists()


# ── CLI ───────────────────────────────────────────────────────────────────────


def test_cli_check_exit_codes(fake_repo: Path, monkeypatch, capsys):
    installed_sha = up._current_sha()
    monkeypatch.setattr(
        up,
        "get_latest_version",
        lambda **_: up.RemoteVersion(
            owner="x",
            repo="y",
            branch="main",
            latest_sha=installed_sha,
            latest_short_sha=installed_sha[:7],
            latest_committed_at="2030-01-01T00:00:00Z",
            latest_message="initial",
            api_url="https://api.github.com/repos/x/y/commits/main",
        ),
    )
    rc = up.main(["check"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "Up to date" in captured.out


def test_cli_check_json_when_behind(fake_repo: Path, monkeypatch, capsys):
    fake_sha = "f" * 40
    monkeypatch.setattr(
        up,
        "get_latest_version",
        lambda **_: up.RemoteVersion(
            owner="x",
            repo="y",
            branch="main",
            latest_sha=fake_sha,
            latest_short_sha=fake_sha[:7],
            latest_committed_at="2030-01-01T00:00:00Z",
            latest_message="newer",
            api_url="https://api.github.com/repos/x/y/commits/main",
        ),
    )
    rc = up.main(["--json", "check"])
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["update_available"] is True
    assert payload["remote"]["latest_sha"] == fake_sha


def test_cli_check_network_error_exit_code(fake_repo: Path, monkeypatch, capsys):
    monkeypatch.setattr(
        up,
        "get_latest_version",
        lambda **_: up.RemoteVersion(
            owner="x",
            repo="y",
            branch="main",
            latest_sha=None,
            latest_short_sha=None,
            latest_committed_at=None,
            latest_message=None,
            api_url="https://api.github.com/repos/x/y/commits/main",
            error="network error: offline",
        ),
    )
    rc = up.main(["check"])
    assert rc == 2
    assert "error" in capsys.readouterr().out


def test_cli_update_skips_when_already_current(fake_repo: Path, monkeypatch, capsys):
    installed_sha = up._current_sha()
    monkeypatch.setattr(
        up,
        "get_latest_version",
        lambda **_: up.RemoteVersion(
            owner="x",
            repo="y",
            branch="main",
            latest_sha=installed_sha,
            latest_short_sha=installed_sha[:7],
            latest_committed_at=None,
            latest_message=None,
            api_url="https://api.github.com/repos/x/y/commits/main",
        ),
    )
    rc = up.main(["update", "--yes"])
    assert rc == 0
    assert "Already up to date" in capsys.readouterr().out
