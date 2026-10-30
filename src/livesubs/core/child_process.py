"""UI-side handle of a child process that talks through two multiprocessing queues.

Used for the engine and the device host. ``target(commands, events)`` runs in a spawned
process; the UI sends commands, polls events without blocking and watches ``alive``.
"""

from __future__ import annotations

import multiprocessing
import queue
from collections.abc import Callable
from multiprocessing.queues import Queue

from livesubs.core.messages import Shutdown


class ChildProcess[C, E]:
    def __init__(self, target: Callable[[Queue, Queue], None], name: str) -> None:
        self._target = target
        self._name = name
        self._create()

    def _create(self) -> None:
        context = multiprocessing.get_context("spawn")
        self._commands: Queue = context.Queue()
        self._events: Queue = context.Queue()
        self._process = context.Process(
            target=self._target,
            args=(self._commands, self._events),
            name=self._name,
            daemon=True,
        )

    def start(self) -> None:
        self._process.start()

    def restart(self) -> None:
        """A fresh process (after a crash), with new queues: the old ones may be broken."""
        if self._process.is_alive():
            self._process.terminate()
            self._process.join(1.0)
        self._create()
        self.start()

    @property
    def started(self) -> bool:
        return self._process.pid is not None

    @property
    def alive(self) -> bool:
        return self._process.is_alive()

    @property
    def pid(self) -> int | None:
        return self._process.pid

    def send(self, command: C) -> None:
        self._commands.put(command)

    def poll(self, max_events: int = 100) -> list[E]:
        """Events received so far, without blocking."""
        received: list[E] = []
        while len(received) < max_events:
            try:
                received.append(self._events.get_nowait())
            except queue.Empty:
                break
        return received

    def wait_event(self, timeout: float) -> E | None:
        try:
            return self._events.get(timeout=timeout)
        except queue.Empty:
            return None

    def shutdown(self, timeout: float = 5.0) -> None:
        if self._process.is_alive():
            self._commands.put(Shutdown())
            self._process.join(timeout)
        if self._process.is_alive():
            self._process.terminate()
            self._process.join(1.0)
