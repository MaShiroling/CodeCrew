import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import app.storage  # noqa: F401 - initialize existing package import order before workspace.
from app.agents.kimi_boundary import KimiBoundaryError, KimiWriteBoundary
from app.workspace.permissions import PermissionPolicy


def make_boundary(
    tmp_path: Path, *, allowed_paths: tuple[str, ...] = ("app",)
) -> KimiWriteBoundary:
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
    assert f'(deny file-read-data (require-all (subpath "{boundary.protected_home}")' in profile
    assert f'(require-not (subpath "{boundary.worktree}"))' in profile
    assert f'(allow file-write* (subpath "{boundary.worktree / "app"}"))' in profile
    assert f'(allow file-write* (subpath "{boundary.runtime_directory}"))' in profile
    assert f'(deny file-write* (subpath "{boundary.worktree / ".git"}"))' in profile


def test_clarification_profile_has_no_worktree_write_exception(tmp_path):
    boundary = make_boundary(tmp_path, allowed_paths=(".",))
    boundary.worktree_read_only = True
    profile = boundary.profile()
    assert f'(deny file-write* (subpath "{boundary.worktree}"))' in profile
    assert f'(allow file-write* (subpath "{boundary.worktree}"))' not in profile
    assert f'(allow file-write* (subpath "{boundary.runtime_directory}"))' in profile


@pytest.mark.skipif(platform.system() != "Darwin", reason="Seatbelt is macOS-only")
def test_empty_standalone_chat_workspace_is_readonly_under_seatbelt(tmp_path: Path) -> None:
    workspace = tmp_path / "empty-chat"
    workspace.mkdir()
    runtime = tmp_path / "private-runtime"
    runtime.mkdir()
    boundary = KimiWriteBoundary(
        worktree=workspace, runtime_directory=runtime,
        policy=PermissionPolicy(allowed_paths=("src",)), worktree_read_only=True,
    )
    for target, allowed in (
        (workspace / "blocked.txt", False),
        (runtime / "allowed.txt", True),
    ):
        result = subprocess.run(
            boundary.wrap([
                "/usr/bin/python3", "-c",
                "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('probe')",
                str(target),
            ]),
            cwd=workspace, capture_output=True, text=True, timeout=10, check=False,
        )
        assert (result.returncode == 0) is allowed, result.stderr


@pytest.mark.skipif(platform.system() != "Darwin", reason="Seatbelt is macOS-only")
def test_seatbelt_clarification_can_read_and_write_runtime_but_cannot_mutate_worktree(tmp_path):
    boundary = make_boundary(tmp_path, allowed_paths=(".",))
    boundary.worktree_read_only = True
    source = boundary.worktree / "app/source.txt"
    source.write_text("original")
    probes = [
        ("assert p.read_text() == 'original'", source, True),
        ("p.write_text('changed')", source, False),
        ("p.write_text('created')", boundary.worktree / "app/new.txt", False),
        ("p.unlink()", source, False),
        ("p.rename(p.with_name('renamed.txt'))", source, False),
        ("p.write_text('runtime')", boundary.runtime_directory / "session.txt", True),
    ]
    for operation, target, allowed in probes:
        command = boundary.wrap([
            "/usr/bin/python3", "-c",
            "from pathlib import Path; import sys; p=Path(sys.argv[1]); " + operation, str(target),
        ])
        result = subprocess.run(
            command, cwd=boundary.worktree, capture_output=True, text=True, timeout=10, check=False,
        )
        assert (result.returncode == 0) is allowed, result.stderr
        assert source.read_text() == "original"
    assert not (source.parent / "new.txt").exists()
    assert not (source.parent / "renamed.txt").exists()


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
        Path(__file__).resolve().parents[1]
        / "app"
        / "agents"
        / "assets"
        / "kimi_restricted_implementer.md"
    ).read_text(encoding="utf-8")
    frontmatter = profile.split("---", maxsplit=2)[1]
    tools_section = frontmatter.split("tools:", maxsplit=1)[1].split("subagents:", maxsplit=1)[0]
    assert {line.strip() for line in tools_section.splitlines() if line.strip()} == {
        "- Read",
        "- Grep",
        "- Glob",
        "- Write",
        "- Edit",
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
                "/usr/bin/python3",
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


@pytest.mark.skipif(platform.system() != "Darwin", reason="Seatbelt is macOS-only")
def test_seatbelt_blocks_real_home_reads_but_allows_worktree(tmp_path: Path) -> None:
    boundary = make_boundary(tmp_path)
    inside = boundary.worktree / "app" / "readable.txt"
    inside.write_text("public", encoding="utf-8")
    protected = boundary.protected_home / ".ssh"
    if not protected.is_dir():
        pytest.skip("no home .ssh directory to probe")
    for target, should_succeed in ((inside, True), (protected, False)):
        result = subprocess.run(
            boundary.wrap(
                [
                    "/usr/bin/python3",
                    "-c",
                    "from pathlib import Path; import sys; Path(sys.argv[1]).read_text()",
                    str(target),
                ]
            ),
            cwd=boundary.worktree,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert (result.returncode == 0) is should_succeed


@pytest.mark.skipif(
    platform.system() != "Darwin" or shutil.which("kimi") is None,
    reason="requires macOS Seatbelt and an installed Kimi CLI",
)
def test_kimi_binary_can_start_inside_write_boundary_without_model_call(tmp_path: Path) -> None:
    binary = Path(shutil.which("kimi"))
    original = make_boundary(tmp_path)
    boundary = KimiWriteBoundary(
        worktree=original.worktree,
        runtime_directory=original.runtime_directory,
        policy=original.policy,
        readable_files=(binary,),
    )
    result = subprocess.run(
        boundary.wrap([str(binary), "--version"]),
        cwd=boundary.worktree,
        env={
            **os.environ,
            "HOME": str(boundary.runtime_directory),
            "KIMI_CODE_HOME": str(boundary.runtime_directory),
            "TMPDIR": str(boundary.runtime_directory),
        },
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip()


@pytest.mark.skipif(platform.system() != "Darwin", reason="Seatbelt is macOS-only")
def test_seatbelt_grants_only_one_readonly_artifact_not_its_siblings(tmp_path: Path) -> None:
    original = make_boundary(tmp_path)
    artifacts = tmp_path / "artifact-home"
    artifacts.mkdir()
    plan, sibling = artifacts / "plan.json", artifacts / "other.json"
    plan.write_text("approved plan", encoding="utf-8")
    sibling.write_text("unrelated evidence", encoding="utf-8")
    boundary = KimiWriteBoundary(
        worktree=original.worktree,
        runtime_directory=original.runtime_directory,
        policy=original.policy,
        read_only_files=(plan,),
    )
    # Protect a disposable synthetic home; never create probes in the user's home.
    boundary.protected_home = artifacts
    for target, operation, expected in (
        (plan, "read_text()", True),
        (sibling, "read_text()", False),
        (plan, "write_text('tampered')", False),
        (plan, "unlink()", False),
    ):
        result = subprocess.run(
            boundary.wrap(
                [
                    "/usr/bin/python3",
                    "-c",
                    f"from pathlib import Path; import sys; Path(sys.argv[1]).{operation}",
                    str(target),
                ]
            ),
            cwd=boundary.worktree,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert (result.returncode == 0) is expected, result.stderr
    assert plan.read_text(encoding="utf-8") == "approved plan"
