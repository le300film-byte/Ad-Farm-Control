"""Scheduler — the only component that owns an event loop timer.

Jobs are plain callables (sync or async). Each job runs in isolation: an exception is logged
and reported through ``on_error`` and never cancels the other jobs. Sync jobs are executed in a
thread so blocking GitHub calls never stall the Discord gateway heartbeat.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

log = logging.getLogger(__name__)

Job = Callable[[], Any]


@dataclass
class ScheduledJob:
    name: str
    interval: float
    job: Job
    run_immediately: bool = False
    last_run: float = 0.0
    last_error: str = ""
    runs: int = 0
    task: Optional[asyncio.Task] = field(default=None, repr=False)


class Scheduler:
    def __init__(self, *, on_error: Callable[[str, BaseException], Awaitable[None] | None] | None = None):
        self._jobs: dict[str, ScheduledJob] = {}
        self._on_error = on_error
        self._running = False

    def add(self, name: str, interval: float, job: Job, *, run_immediately: bool = False) -> ScheduledJob:
        entry = ScheduledJob(name=name, interval=float(interval), job=job, run_immediately=run_immediately)
        self._jobs[name] = entry
        if self._running:
            entry.task = asyncio.get_event_loop().create_task(self._loop(entry), name=f"job:{name}")
        return entry

    def jobs(self) -> list[ScheduledJob]:
        return list(self._jobs.values())

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        loop = asyncio.get_event_loop()
        for entry in self._jobs.values():
            entry.task = loop.create_task(self._loop(entry), name=f"job:{entry.name}")

    async def stop(self) -> None:
        self._running = False
        for entry in self._jobs.values():
            if entry.task:
                entry.task.cancel()
        for entry in self._jobs.values():
            if entry.task:
                try:
                    await entry.task
                except (asyncio.CancelledError, Exception):
                    pass
                entry.task = None

    async def run_once(self, name: str) -> Any:
        return await self._execute(self._jobs[name])

    async def _loop(self, entry: ScheduledJob) -> None:
        if not entry.run_immediately:
            await asyncio.sleep(entry.interval)
        while self._running:
            await self._execute(entry)
            await asyncio.sleep(entry.interval)

    async def _execute(self, entry: ScheduledJob) -> Any:
        started = time.monotonic()
        try:
            result = entry.job()
            if inspect.isawaitable(result):
                result = await result
            elif not asyncio.iscoroutinefunction(entry.job):
                pass
            entry.last_error = ""
            return result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            entry.last_error = f"{type(exc).__name__}: {exc}"
            log.exception("scheduled job %s failed", entry.name)
            if self._on_error:
                try:
                    maybe = self._on_error(entry.name, exc)
                    if inspect.isawaitable(maybe):
                        await maybe
                except Exception:  # pragma: no cover - alert failure must not loop
                    log.exception("on_error handler failed for job %s", entry.name)
            return None
        finally:
            entry.runs += 1
            entry.last_run = time.time()
            elapsed = time.monotonic() - started
            if elapsed > max(30.0, entry.interval / 2):
                log.warning("job %s took %.1fs (interval %.0fs)", entry.name, elapsed, entry.interval)


def in_thread(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Callable[[], Awaitable[Any]]:
    """Wrap a blocking callable so the scheduler runs it off the event loop."""

    async def runner() -> Any:
        return await asyncio.to_thread(fn, *args, **kwargs)

    runner.__name__ = getattr(fn, "__name__", "job")
    return runner
