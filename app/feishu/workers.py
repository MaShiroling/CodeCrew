"""Confirm process cleanup even when an application shutdown is cancelled."""

import asyncio


async def finish_cleanup(cleanup) -> None:
    task = asyncio.create_task(cleanup)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    task.result()
    if cancelled:
        raise asyncio.CancelledError


async def stop_process(process) -> None:
    if process.is_alive():
        process.terminate()
    await asyncio.to_thread(process.join, 3)
    if process.is_alive():
        process.kill()
        await asyncio.to_thread(process.join, 3)
    if process.is_alive():
        raise RuntimeError("sdk_stop_unconfirmed")
    process.close()
