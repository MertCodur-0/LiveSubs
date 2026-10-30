"""UI-side handle of a child process that talks through two multiprocessing queues.

Used for the engine and the device host. ``target(commands, events)`` runs in a spawned
process; the UI sends commands, polls events without blocking and watches ``alive``.
"""

from __future__ import annotations

import logging
import multiprocessing
import multiprocessing.connection
import os
import queue
import threading
from collections.abc import Callable
from multiprocessing.queues import Queue

from livesubs.core.messages import Shutdown

log = logging.getLogger(__name__)


def exit_with_parent() -> None:
    """In a child: end this process as soon as the UI process is gone.

    ``daemon=True`` only covers a clean exit of the parent; after a crash or a kill the children
    kept running (seen with the packaged exe): the device host would keep the card locked and
    its preview on screen. Windows releases the card, shared memory and power requests of an
    exited process, so a hard exit is enough.
    """
    parent = multiprocessing.parent_process()
    if parent is None:
        return

    def watch() -> None:
        multiprocessing.connection.wait([parent.sentinel])
        log.warning("The UI process is gone; exiting")
        logging.shutdown()
        os._exit(0)

    threading.Thread(target=watch, name="parent-watch", daemon=True).start()


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
