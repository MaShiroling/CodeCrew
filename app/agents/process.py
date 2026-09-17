import asyncio
import os
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from time import monotonic


class ProcessRunnerError(RuntimeError):
    """Base error raised by the subprocess boundary."""


class ProcessStartError(ProcessRunnerError):
    """Raised when the operating system cannot start a process."""


class ProcessStream(str, Enum):
    STDOUT = "stdout"
    STDERR = "stderr"


@dataclass(frozen=True, slots=True)
class ProcessChunk:
    stream: ProcessStream
    text: str


@dataclass(frozen=True, slots=True)
class ProcessResult:
    exit_code: int
    duration_ms: int
    timed_out: bool = False
    cancelled: bool = False
    dropped_chunks: int = 0


_END_OF_STREAM = object()


class ManagedProcess:
    """A running subprocess with bounded output streaming and deterministic cleanup."""

    def __init__(
        self,
        process: asyncio.subprocess.Process,
        *,
        timeout_seconds: float,
        terminate_grace_seconds: float,
        queue_maxsize: int,
    ) -> None:
        self._process = process
        self._timeout_seconds = timeout_seconds
        self._terminate_grace_seconds = terminate_grace_seconds
        self._queue: asyncio.Queue[ProcessChunk | object] = asyncio.Queue(queue_maxsize)
        self._started_at = monotonic()
        self._cancelled = False
        self._timed_out = False
        self._dropped_chunks = 0
        self._stream_claimed = False
        self._stop_lock = asyncio.Lock()
        self._stdout_task = asyncio.create_task(
            self._pump(process.stdout, ProcessStream.STDOUT)
        )
        self._stderr_task = asyncio.create_task(
            self._pump(process.stderr, ProcessStream.STDERR)
        )
        self._completion_task = asyncio.create_task(self._monitor())

    @property
    def pid(self) -> int:
        return self._process.pid

    @property
    def returncode(self) -> int | None:
        return self._process.returncode

    async def stream(self) -> AsyncIterator[ProcessChunk]:
        """Consume process output once. Slow consumers receive the newest bounded window."""

        if self._stream_claimed:
            raise ProcessRunnerError("process output stream can only be consumed once")
        self._stream_claimed = True

        while True:
            item = await self._queue.get()
            if item is _END_OF_STREAM:
                return
            if isinstance(item, ProcessChunk):
                yield item

    async def wait(self) -> ProcessResult:
        """Return the cached final result; repeated waits are safe."""

        return await asyncio.shield(self._completion_task)

    async def cancel(self) -> None:
        """Terminate the process if needed; repeated cancellation is safe."""

        if self._completion_task.done():
            return
        self._cancelled = True
        await self._stop_process()
        await self.wait()

    async def _pump(
        self,
        reader: asyncio.StreamReader | None,
        stream: ProcessStream,
    ) -> None:
        if reader is None:
            return
        while line := await reader.readline():
            self._enqueue(ProcessChunk(stream=stream, text=line.decode(errors="replace")))

    async def _monitor(self) -> ProcessResult:
        try:
            await asyncio.wait_for(self._process.wait(), timeout=self._timeout_seconds)
        except TimeoutError:
            self._timed_out = True
            await self._stop_process()

        await asyncio.gather(self._stdout_task, self._stderr_task)
        duration_ms = max(0, int((monotonic() - self._started_at) * 1000))
        result = ProcessResult(
            exit_code=self._process.returncode,
            duration_ms=duration_ms,
            timed_out=self._timed_out,
            cancelled=self._cancelled,
            dropped_chunks=self._dropped_chunks,
        )
        self._enqueue(_END_OF_STREAM)
        return result

    async def _stop_process(self) -> None:
        async with self._stop_lock:
            if self._process.returncode is not None:
                return
            self._process.terminate()
            try:
                await asyncio.wait_for(
                    self._process.wait(), timeout=self._terminate_grace_seconds
                )
            except TimeoutError:
                self._process.kill()
                await self._process.wait()

    def _enqueue(self, item: ProcessChunk | object) -> None:
        if self._queue.full():
            self._queue.get_nowait()
            self._dropped_chunks += 1
        self._queue.put_nowait(item)


class AsyncProcessRunner:
    """Starts subprocesses without shell interpretation."""

    def __init__(self, *, queue_maxsize: int = 256, terminate_grace_seconds: float = 2.0) -> None:
        if queue_maxsize <= 0:
            raise ValueError("queue_maxsize must be positive")
        if terminate_grace_seconds <= 0:
            raise ValueError("terminate_grace_seconds must be positive")
        self._queue_maxsize = queue_maxsize
        self._terminate_grace_seconds = terminate_grace_seconds

    async def start(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: float,
        env: Mapping[str, str] | None = None,
    ) -> ManagedProcess:
        if not argv:
            raise ValueError("argv must not be empty")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

        process_env = None if env is None else {**os.environ, **env}
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=cwd,
                env=process_env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise ProcessStartError(f"failed to start executable {argv[0]!r}: {exc}") from exc

        return ManagedProcess(
            process,
            timeout_seconds=timeout_seconds,
            terminate_grace_seconds=self._terminate_grace_seconds,
            queue_maxsize=self._queue_maxsize,
        )
