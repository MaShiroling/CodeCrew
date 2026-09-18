import asyncio
import os
from enum import Enum
from pathlib import Path, PurePosixPath
from time import monotonic
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from app.orchestration.models import utc_now
from app.storage import ArtifactReference, ArtifactStore, ArtifactType
from app.workspace.worktrees import WorktreeHandle


class CommandExecutionError(RuntimeError):
    """Raised when command evidence does not belong to the requested worktree."""


class CommandStatus(str, Enum):
    DENIED = "denied"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    START_FAILED = "start_failed"


class CommandRule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    argv_prefix: tuple[str, ...] = Field(min_length=1)
    allow_extra_args: bool = True

    @field_validator("argv_prefix")
    @classmethod
    def validate_prefix(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item or "\0" in item for item in value):
            raise ValueError("command rule arguments must not be empty or contain null bytes")
        return value

    def matches(self, argv: tuple[str, ...]) -> bool:
        if argv[: len(self.argv_prefix)] != self.argv_prefix:
            return False
        return self.allow_extra_args or len(argv) == len(self.argv_prefix)


class CommandPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", validate_default=True)

    rules: tuple[CommandRule, ...] = Field(min_length=1)
    allowed_working_directories: tuple[str, ...] = (".",)
    allowed_environment: tuple[str, ...] = ()
    inherited_environment: tuple[str, ...] = (
        "PATH",
        "LANG",
        "LC_ALL",
        "TMPDIR",
        "TEMP",
        "TMP",
        "SYSTEMROOT",
    )
    max_timeout_seconds: float = Field(default=900, gt=0)
    max_output_bytes: int = Field(default=1024 * 1024, gt=0)

    @field_validator("allowed_working_directories")
    @classmethod
    def validate_working_directories(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if not values:
            raise ValueError("at least one working directory must be allowed")
        normalized: list[str] = []
        for value in values:
            path = PurePosixPath(value)
            if (
                not value
                or "\\" in value
                or path.is_absolute()
                or ".." in path.parts
            ):
                raise ValueError("working directories must be repository-relative")
            rendered = path.as_posix()
            if rendered not in normalized:
                normalized.append(rendered)
        return tuple(normalized)

    @field_validator("allowed_environment", "inherited_environment")
    @classmethod
    def validate_environment_names(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not name or not name.replace("_", "a").isalnum() for name in values):
            raise ValueError("allowed environment names must be identifiers")
        return tuple(dict.fromkeys(values))

    @classmethod
    def coding_defaults(cls) -> "CommandPolicy":
        return cls(
            rules=(
                CommandRule(name="pytest", argv_prefix=("pytest",)),
                CommandRule(name="python-pytest", argv_prefix=("python", "-m", "pytest")),
                CommandRule(name="ruff-check", argv_prefix=("ruff", "check")),
                CommandRule(name="mypy", argv_prefix=("mypy",)),
                CommandRule(name="npm-test", argv_prefix=("npm", "test")),
                CommandRule(name="npm-run-test", argv_prefix=("npm", "run", "test")),
                CommandRule(name="cargo-test", argv_prefix=("cargo", "test")),
                CommandRule(name="go-test", argv_prefix=("go", "test")),
                CommandRule(name="cmake-build", argv_prefix=("cmake", "--build")),
                CommandRule(name="ctest", argv_prefix=("ctest",)),
            )
        )


class CommandRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    command_id: UUID = Field(default_factory=uuid4)
    task_id: UUID
    trace_id: UUID
    argv: tuple[str, ...] = Field(min_length=1)
    working_directory: str = "."
    timeout_seconds: float | None = Field(default=None, gt=0)
    environment: dict[str, str] = Field(default_factory=dict)

    @field_validator("argv")
    @classmethod
    def validate_argv(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item or "\0" in item for item in value):
            raise ValueError("command arguments must not be empty or contain null bytes")
        return value

    @field_validator("working_directory")
    @classmethod
    def validate_working_directory(cls, value: str) -> str:
        path = PurePosixPath(value)
        if not value or "\\" in value or path.is_absolute() or ".." in path.parts:
            raise ValueError("working directory must be repository-relative")
        return path.as_posix()

    @field_validator("environment")
    @classmethod
    def validate_environment(cls, value: dict[str, str]) -> dict[str, str]:
        if any(not key or "=" in key or "\0" in key for key in value):
            raise ValueError("environment names must be valid process environment keys")
        if any("\0" in item for item in value.values()):
            raise ValueError("environment values must not contain null bytes")
        return value


class CommandResult(BaseModel):
    """Audited process facts; success here does not complete the software task."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command_id: UUID
    task_id: UUID
    trace_id: UUID
    argv: tuple[str, ...]
    working_directory: str
    status: CommandStatus
    matched_rule: str | None = None
    exit_code: int | None = None
    duration_ms: int = Field(ge=0)
    denial_reason: str | None = None
    stdout_artifact: ArtifactReference | None = None
    stderr_artifact: ArtifactReference | None = None
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    audit_artifact: ArtifactReference
    started_at: AwareDatetime
    finished_at: AwareDatetime


class CommandExecutor:
    """Policy-gated subprocess execution with bounded output and durable audit evidence."""

    _SHELL_TOKENS = frozenset({"|", "||", "&&", ";", "<", ">", ">>"})
    _SHELL_EXECUTABLES = frozenset({"sh", "bash", "zsh", "fish", "cmd", "powershell", "pwsh"})

    def __init__(self, artifacts: ArtifactStore, policy: CommandPolicy) -> None:
        self.artifacts = artifacts
        self.policy = policy

    async def execute(self, handle: WorktreeHandle, request: CommandRequest) -> CommandResult:
        if request.task_id != handle.task_id:
            raise CommandExecutionError("command request belongs to another task")
        started_at = utc_now()
        started_clock = monotonic()
        rule, denial_reason, cwd = self._authorize(handle, request)
        if denial_reason is not None:
            return self._persist_result(
                request,
                status=CommandStatus.DENIED,
                started_at=started_at,
                started_clock=started_clock,
                matched_rule=rule.name if rule else None,
                denial_reason=denial_reason,
            )

        timeout = min(
            request.timeout_seconds or self.policy.max_timeout_seconds,
            self.policy.max_timeout_seconds,
        )
        process_environment = {
            key: os.environ[key]
            for key in self.policy.inherited_environment
            if key in os.environ
        }
        process_environment.update(request.environment)
        try:
            process = await asyncio.create_subprocess_exec(
                *request.argv,
                cwd=cwd,
                env=process_environment,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            return self._persist_result(
                request,
                status=CommandStatus.START_FAILED,
                started_at=started_at,
                started_clock=started_clock,
                matched_rule=rule.name,
                denial_reason=f"failed to start command: {exc}",
            )

        stdout_task = asyncio.create_task(self._read_bounded(process.stdout))
        stderr_task = asyncio.create_task(self._read_bounded(process.stderr))
        status: CommandStatus
        try:
            await asyncio.wait_for(process.wait(), timeout=timeout)
            status = (
                CommandStatus.SUCCEEDED if process.returncode == 0 else CommandStatus.FAILED
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            status = CommandStatus.TIMED_OUT
        except asyncio.CancelledError:
            process.kill()
            await process.wait()
            stdout, stdout_truncated = await stdout_task
            stderr, stderr_truncated = await stderr_task
            self._persist_result(
                request,
                status=CommandStatus.CANCELLED,
                started_at=started_at,
                started_clock=started_clock,
                matched_rule=rule.name,
                exit_code=process.returncode,
                stdout=stdout,
                stderr=stderr,
                stdout_truncated=stdout_truncated,
                stderr_truncated=stderr_truncated,
            )
            raise

        stdout, stdout_truncated = await stdout_task
        stderr, stderr_truncated = await stderr_task
        return self._persist_result(
            request,
            status=status,
            started_at=started_at,
            started_clock=started_clock,
            matched_rule=rule.name,
            exit_code=process.returncode,
            stdout=stdout,
            stderr=stderr,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
        )

    def _authorize(
        self, handle: WorktreeHandle, request: CommandRequest
    ) -> tuple[CommandRule | None, str | None, Path]:
        rule = next((item for item in self.policy.rules if item.matches(request.argv)), None)
        worktree = handle.worktree_path.resolve()
        cwd = (worktree / request.working_directory).resolve()
        if rule is None:
            return None, "command does not match any allowlist rule", cwd
        if Path(request.argv[0]).name.lower() in self._SHELL_EXECUTABLES:
            return rule, "interactive shells and shell command strings are forbidden", cwd
        if not worktree.is_dir():
            return rule, "managed worktree does not exist", cwd
        if not cwd.is_dir() or not cwd.is_relative_to(worktree):
            return rule, "working directory escapes or does not exist in worktree", cwd
        if not any(
            self._path_matches(request.working_directory, allowed)
            for allowed in self.policy.allowed_working_directories
        ):
            return rule, "working directory is outside allowed roots", cwd
        unknown_environment = set(request.environment) - set(self.policy.allowed_environment)
        if unknown_environment:
            names = ", ".join(sorted(unknown_environment))
            return rule, f"environment variables are not allowed: {names}", cwd
        unsafe = next((argument for argument in request.argv[1:] if self._unsafe(argument)), None)
        if unsafe is not None:
            return rule, f"unsafe shell or path argument is not allowed: {unsafe!r}", cwd
        return rule, None, cwd

    async def _read_bounded(
        self, stream: asyncio.StreamReader | None
    ) -> tuple[bytes, bool]:
        if stream is None:
            return b"", False
        retained = bytearray()
        truncated = False
        while chunk := await stream.read(64 * 1024):
            remaining = self.policy.max_output_bytes - len(retained)
            if remaining > 0:
                retained.extend(chunk[:remaining])
            if len(chunk) > remaining:
                truncated = True
        return bytes(retained), truncated

    def _persist_result(
        self,
        request: CommandRequest,
        *,
        status: CommandStatus,
        started_at: AwareDatetime,
        started_clock: float,
        matched_rule: str | None,
        exit_code: int | None = None,
        denial_reason: str | None = None,
        stdout: bytes = b"",
        stderr: bytes = b"",
        stdout_truncated: bool = False,
        stderr_truncated: bool = False,
    ) -> CommandResult:
        finished_at = utc_now()
        duration_ms = max(0, int((monotonic() - started_clock) * 1000))
        stdout_reference = self._persist_output(request, stdout, "stdout", stdout_truncated)
        stderr_reference = self._persist_output(request, stderr, "stderr", stderr_truncated)
        audit = {
            "command_id": str(request.command_id),
            "task_id": str(request.task_id),
            "trace_id": str(request.trace_id),
            "argv": list(request.argv),
            "working_directory": request.working_directory,
            "status": status.value,
            "matched_rule": matched_rule,
            "exit_code": exit_code,
            "duration_ms": duration_ms,
            "denial_reason": denial_reason,
            "environment_names": sorted(request.environment),
            "stdout_artifact_id": (
                str(stdout_reference.artifact_id) if stdout_reference else None
            ),
            "stderr_artifact_id": (
                str(stderr_reference.artifact_id) if stderr_reference else None
            ),
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
        }
        metadata = self.artifacts.put_json(
            audit,
            task_id=request.task_id,
            trace_id=request.trace_id,
            type=ArtifactType.COMMAND_AUDIT,
            created_by="command_executor",
            filename=f"command-{request.command_id}.json",
            metadata={"status": status.value},
        )
        audit_reference = ArtifactReference.from_metadata(
            metadata, summary=f"Command execution audit: {status.value}"
        )
        return CommandResult(
            command_id=request.command_id,
            task_id=request.task_id,
            trace_id=request.trace_id,
            argv=request.argv,
            working_directory=request.working_directory,
            status=status,
            matched_rule=matched_rule,
            exit_code=exit_code,
            duration_ms=duration_ms,
            denial_reason=denial_reason,
            stdout_artifact=stdout_reference,
            stderr_artifact=stderr_reference,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
            audit_artifact=audit_reference,
            started_at=started_at,
            finished_at=finished_at,
        )

    def _persist_output(
        self,
        request: CommandRequest,
        content: bytes,
        stream: str,
        truncated: bool,
    ) -> ArtifactReference | None:
        if not content:
            return None
        metadata = self.artifacts.put_bytes(
            content,
            task_id=request.task_id,
            trace_id=request.trace_id,
            type=ArtifactType.TEST_LOG,
            media_type="text/plain; charset=utf-8",
            created_by="command_executor",
            filename=f"command-{request.command_id}-{stream}.log",
            metadata={"stream": stream, "truncated": str(truncated).lower()},
        )
        return ArtifactReference.from_metadata(
            metadata, summary=f"Command {stream} output"
        )

    @classmethod
    def _unsafe(cls, argument: str) -> bool:
        if argument in cls._SHELL_TOKENS or "$(" in argument or "`" in argument:
            return True
        candidate = (
            argument.split("=", 1)[1]
            if argument.startswith("-") and "=" in argument
            else argument
        )
        if candidate.startswith(("/", "\\", "~")):
            return True
        return ".." in PurePosixPath(candidate).parts

    @staticmethod
    def _path_matches(path_value: str, rule_value: str) -> bool:
        if rule_value == ".":
            return True
        path = PurePosixPath(path_value)
        rule = PurePosixPath(rule_value)
        return path == rule or rule in path.parents
