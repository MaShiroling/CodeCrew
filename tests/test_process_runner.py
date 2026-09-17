import sys
from pathlib import Path

import pytest

from app.agents.process import (
    AsyncProcessRunner,
    ProcessRunnerError,
    ProcessStartError,
    ProcessStream,
)


@pytest.mark.asyncio
async def test_streams_stdout_stderr_and_reports_exit_code(tmp_path: Path) -> None:
    process = await AsyncProcessRunner().start(
        [
            sys.executable,
            "-c",
            "import sys; print('out'); print('err', file=sys.stderr); raise SystemExit(7)",
        ],
        cwd=tmp_path,
        timeout_seconds=5,
    )

    chunks = [chunk async for chunk in process.stream()]
    result = await process.wait()

    assert {(chunk.stream, chunk.text) for chunk in chunks} == {
        (ProcessStream.STDOUT, "out\n"),
        (ProcessStream.STDERR, "err\n"),
    }
    assert result.exit_code == 7
    assert not result.timed_out
    assert not result.cancelled


@pytest.mark.asyncio
async def test_timeout_terminates_process(tmp_path: Path) -> None:
    process = await AsyncProcessRunner(terminate_grace_seconds=0.2).start(
        [sys.executable, "-c", "import time; time.sleep(10)"],
        cwd=tmp_path,
        timeout_seconds=0.05,
    )

    result = await process.wait()

    assert result.timed_out
    assert not result.cancelled
    assert process.returncode is not None


@pytest.mark.asyncio
async def test_cancel_is_idempotent_and_wait_is_repeatable(tmp_path: Path) -> None:
    process = await AsyncProcessRunner(terminate_grace_seconds=0.2).start(
        [sys.executable, "-c", "import time; time.sleep(10)"],
        cwd=tmp_path,
        timeout_seconds=5,
    )

    await process.cancel()
    await process.cancel()
    first = await process.wait()
    second = await process.wait()

    assert first == second
    assert first.cancelled
    assert not first.timed_out
    assert process.returncode is not None


@pytest.mark.asyncio
async def test_start_failure_is_normalized(tmp_path: Path) -> None:
    with pytest.raises(ProcessStartError, match="failed to start executable"):
        await AsyncProcessRunner().start(
            ["definitely-not-a-codecrew-executable"],
            cwd=tmp_path,
            timeout_seconds=5,
        )


@pytest.mark.asyncio
async def test_bounded_queue_drops_old_output_without_blocking_wait(tmp_path: Path) -> None:
    process = await AsyncProcessRunner(queue_maxsize=3).start(
        [sys.executable, "-c", "for value in range(100): print(value)"],
        cwd=tmp_path,
        timeout_seconds=5,
    )

    result = await process.wait()
    chunks = [chunk async for chunk in process.stream()]

    assert result.exit_code == 0
    assert result.dropped_chunks > 0
    assert len(chunks) <= 2


@pytest.mark.asyncio
async def test_output_stream_has_a_single_consumer(tmp_path: Path) -> None:
    process = await AsyncProcessRunner().start(
        [sys.executable, "-c", "print('safe')"],
        cwd=tmp_path,
        timeout_seconds=5,
    )
    assert [chunk.text async for chunk in process.stream()] == ["safe\n"]

    with pytest.raises(ProcessRunnerError, match="only be consumed once"):
        _ = [chunk async for chunk in process.stream()]


@pytest.mark.asyncio
async def test_arguments_are_not_interpreted_by_a_shell(tmp_path: Path) -> None:
    literal = "$(touch should-not-exist)"
    process = await AsyncProcessRunner().start(
        [sys.executable, "-c", "import sys; print(sys.argv[1])", literal],
        cwd=tmp_path,
        timeout_seconds=5,
    )

    chunks = [chunk async for chunk in process.stream()]
    result = await process.wait()

    assert result.exit_code == 0
    assert [chunk.text for chunk in chunks] == [f"{literal}\n"]
    assert not (tmp_path / "should-not-exist").exists()

