import platform
import subprocess
import sys
from pathlib import Path

import pytest

import app.storage  # noqa: F401 - initialize existing package import order before workspace.
from app.agents.kimi_boundary import KimiBoundaryError, KimiWriteBoundary
from app.workspace.permissions import PermissionPolicy


def make_boundary(tmp_path: Path, *, allowed_paths: tuple[str, ...] = ("app",)) -> KimiWriteBoundary:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "app").mkdir()
    (worktree / "other").mkdir()
    (worktree / ".git").mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    return KimiWriteBoundary(
        worktree=worktree,
        runtime_directory=runtime,
        policy=PermissionPolicy(allowed_paths=allowed_paths),
    )


def test_profile_allows_only_configured_write_roots(tmp_path: Path) -> None:
    boundary = make_boundary(tmp_path)
    profile = boundary.profile()

    assert "(deny file-write*)" in profile
    assert f'(allow file-write* (subpath "{boundary.worktree / "app"}"))' in profile
    assert f'(allow file-write* (subpath "{boundary.runtime_directory}"))' in profile
    assert f'(deny file-write* (subpath "{boundary.worktree / ".git"}"))' in profile


def test_boundary_rejects_symlinked_or_denied_write_root(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (worktree / "link").symlink_to(outside, target_is_directory=True)
    (worktree / ".git").mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()

    with pytest.raises(KimiBoundaryError, match="symlink"):
        KimiWriteBoundary(
            worktree=worktree,
            runtime_directory=runtime,
            policy=PermissionPolicy(allowed_paths=("link",)),
        )
    with pytest.raises(KimiBoundaryError, match="denied"):
        KimiWriteBoundary(
            worktree=worktree,
            runtime_directory=runtime,
            policy=PermissionPolicy(allowed_paths=(".git",)),
        )


def test_boundary_rejects_runtime_inside_worktree_and_missing_sandbox(tmp_path: Path) -> None:
    boundary = make_boundary(tmp_path)
    with pytest.raises(KimiBoundaryError, match="separate"):
        KimiWriteBoundary(
            worktree=boundary.worktree,
            runtime_directory=boundary.worktree / "app",
            policy=boundary.policy,
        )
    missing = KimiWriteBoundary(
        worktree=boundary.worktree,
        runtime_directory=boundary.runtime_directory,
        policy=boundary.policy,
        sandbox_executable=str(tmp_path / "missing-sandbox"),
    )
    with pytest.raises(KimiBoundaryError, match="unavailable|requires macOS"):
        missing.wrap([sys.executable, "-V"])


def test_kimi_agent_profile_exposes_no_command_or_delegation_tool() -> None:
    profile = (
        Path(__file__).resolve().parents[1] / "prompts" / "kimi_restricted_implementer.md"
    ).read_text(encoding="utf-8")
    frontmatter = profile.split("---", maxsplit=2)[1]
    tools_section = frontmatter.split("tools:", maxsplit=1)[1].split("subagents:", maxsplit=1)[0]
    assert {line.strip() for line in tools_section.splitlines() if line.strip()} == {
        "- Read", "- Grep", "- Glob", "- Write", "- Edit"
    }
    assert "subagents: []" in frontmatter


@pytest.mark.skipif(platform.system() != "Darwin", reason="Seatbelt is macOS-only")
def test_seatbelt_rejects_forbidden_writes_before_execution(tmp_path: Path) -> None:
    boundary = make_boundary(tmp_path, allowed_paths=(".",))
    outside = tmp_path / "outside"
    outside.mkdir()
    link = boundary.worktree / "app" / "link"
    link.symlink_to(outside, target_is_directory=True)

    for target, should_succeed in (
        (boundary.worktree / "app" / "allowed.txt", True),
        (boundary.runtime_directory / "session.txt", True),
        (boundary.worktree / ".git" / "blocked.txt", False),
        (boundary.worktree / ".env", False),
        (outside / "blocked.txt", False),
        (link / "blocked-via-link.txt", False),
    ):
        command = boundary.wrap(
            [
                sys.executable,
                "-c",
                "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('probe')",
                str(target),
            ]
        )
        result = subprocess.run(
            command,
            cwd=boundary.worktree,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert (result.returncode == 0) is should_succeed, result.stderr
        assert target.exists() is should_succeed
