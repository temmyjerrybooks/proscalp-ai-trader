from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass


@dataclass(slots=True)
class ScheduledTask:
    name: str
    interval_seconds: int
    running: bool = False


class SimpleScheduler:
    """Small asyncio scheduler used by the MVP without adding a heavy dependency."""

    def __init__(self) -> None:
        self.tasks: dict[str, asyncio.Task] = {}
        self.definitions: dict[str, ScheduledTask] = {}

    def start(self, name: str, interval_seconds: int, coroutine_factory: Callable[[], Awaitable[None]]) -> None:
        if name in self.tasks and not self.tasks[name].done():
            return
        self.definitions[name] = ScheduledTask(name, interval_seconds, True)
        self.tasks[name] = asyncio.create_task(self._run(name, interval_seconds, coroutine_factory))

    async def stop(self, name: str) -> None:
        task = self.tasks.get(name)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if name in self.definitions:
            self.definitions[name].running = False

    def status(self) -> dict[str, dict]:
        return {
            name: {"interval_seconds": definition.interval_seconds, "running": definition.running}
            for name, definition in self.definitions.items()
        }

    async def _run(
        self,
        name: str,
        interval_seconds: int,
        coroutine_factory: Callable[[], Awaitable[None]],
    ) -> None:
        try:
            while True:
                await coroutine_factory()
                await asyncio.sleep(interval_seconds)
        finally:
            if name in self.definitions:
                self.definitions[name].running = False


scheduler = SimpleScheduler()
