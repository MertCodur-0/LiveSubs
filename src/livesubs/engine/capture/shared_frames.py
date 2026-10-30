"""The newest capture-card frame, shared between processes through named shared memory.

The device host owns the card and writes every frame (60 fps); the engine reads the newest one
at its own pace (10 fps), and the UI takes a single frame for region selection. Pixels never go
through a ``multiprocessing.Queue`` (1080p60 BGR is ~370 MB/s).

Layout: a header of int64 fields, per-slot metadata, then ``SLOTS`` frame buffers. The writer
fills the slot after the newest one, so a reader copying the newest frame has ``SLOTS - 1``
frame times before that buffer is reused. Each slot is guarded like a seqlock: the writer sets
``begin``, copies the pixels, then sets ``end``; a reader accepts a copy only when both equal
the sequence number it wanted. x86-64 keeps stores in program order, which this relies on.
``time.monotonic()`` is system-wide on Windows (QPC), so timestamps compare across processes.
"""

from __future__ import annotations

import itertools
import os
import time
from multiprocessing import shared_memory

import numpy as np

from livesubs.core.geometry import Rect
from livesubs.engine.capture.base import (
    Frame,
    SourceInfo,
    SourceKind,
    SourceLostError,
    SourceUnavailableError,
)

BACKEND = "shared"
SLOTS = 3
_MAGIC = 0x4C53_4652_0001  # "LSFR", layout version 1
# header fields (int64)
_H_MAGIC, _H_WIDTH, _H_HEIGHT, _H_CHANNELS, _H_CLOSED, _H_LATEST = range(6)
_HEADER_FIELDS = 8
# per-slot fields (int64; the timestamp is a float64 stored in the same 8 bytes)
_S_BEGIN, _S_END, _S_TIME = range(3)
_SLOT_FIELDS = 3
_POLL_S = 0.002
STALE_AFTER_S = 3.0

_names = itertools.count(1)


def _layout(buffer: memoryview, width: int, height: int, channels: int):
    header = np.ndarray((_HEADER_FIELDS,), np.int64, buffer, 0)
    meta_offset = _HEADER_FIELDS * 8
    meta = np.ndarray((SLOTS, _SLOT_FIELDS), np.int64, buffer, meta_offset)
    times = np.ndarray((SLOTS, _SLOT_FIELDS), np.float64, buffer, meta_offset)
    pixels_offset = meta_offset + SLOTS * _SLOT_FIELDS * 8
    pixels = np.ndarray((SLOTS, height, width, channels), np.uint8, buffer, pixels_offset)
    return header, meta, times, pixels


def _size(width: int, height: int, channels: int) -> int:
    return (_HEADER_FIELDS + SLOTS * _SLOT_FIELDS) * 8 + SLOTS * width * height * channels


class SharedFrameWriter:
    """Creates the shared memory; only the process that owns the device writes to it."""

    def __init__(self, width: int, height: int, channels: int = 3, name: str | None = None):
        self.name = name or f"livesubs-frames-{os.getpid()}-{next(_names)}"
        self.shape = (height, width, channels)
        self._shm = shared_memory.SharedMemory(
            self.name, create=True, size=_size(width, height, channels)
        )
        self._header, self._meta, self._times, self._pixels = _layout(
            self._shm.buf, width, height, channels
        )
        self._header[:] = 0
        self._meta[:] = 0
        self._header[_H_WIDTH] = width
        self._header[_H_HEIGHT] = height
        self._header[_H_CHANNELS] = channels
        self._header[_H_MAGIC] = _MAGIC  # last: readers check it first
        self._seq = 0

    def write(self, image: np.ndarray, timestamp: float) -> None:
        if image.shape != self.shape:
            raise ValueError(f"frame shape {image.shape} != {self.shape}")
        seq = self._seq + 1
        slot = seq % SLOTS
        self._meta[slot, _S_BEGIN] = seq
        np.copyto(self._pixels[slot], image)
        self._times[slot, _S_TIME] = timestamp
        self._meta[slot, _S_END] = seq
        self._header[_H_LATEST] = seq
        self._seq = seq

    def close(self) -> None:
        """Tell readers the device is gone, then drop our handle."""
        if self._shm is None:
            return
        self._header[_H_CLOSED] = 1
        del self._header, self._meta, self._times, self._pixels  # views pin the buffer
        self._shm.close()
        self._shm.unlink()
        self._shm = None


class SharedFrameReader:
    """Attaches to a writer's shared memory by name and copies out the newest frame."""

    def __init__(self, name: str) -> None:
        try:
            # track=False: the resource tracker must not destroy memory another process owns.
            self._shm = shared_memory.SharedMemory(name, track=False)
        except (FileNotFoundError, OSError) as exc:
            raise SourceUnavailableError(f"no shared frames called {name!r}") from exc
        header = np.ndarray((_HEADER_FIELDS,), np.int64, self._shm.buf, 0)
        if header[_H_MAGIC] != _MAGIC:
            del header
            self._shm.close()
            raise SourceUnavailableError(f"{name!r} is not a LiveSubs frame buffer")
        width, height, channels = (int(header[f]) for f in (_H_WIDTH, _H_HEIGHT, _H_CHANNELS))
        del header
        self.shape = (height, width, channels)
        self._header, self._meta, self._times, self._pixels = _layout(
            self._shm.buf, width, height, channels
        )

    @property
    def closed(self) -> bool:
        return bool(self._header[_H_CLOSED])

    @property
    def latest_seq(self) -> int:
        return int(self._header[_H_LATEST])

    def read(self) -> tuple[int, Frame] | None:
        """(sequence number, a private copy of the newest frame); None before the first one."""
        while True:
            seq = int(self._header[_H_LATEST])
            if seq == 0:
                return None
            slot = seq % SLOTS
            if self._meta[slot, _S_END] != seq:
                continue  # the writer is one frame further already; take that one
            image = self._pixels[slot].copy()
            timestamp = float(self._times[slot, _S_TIME])
            if self._meta[slot, _S_BEGIN] == seq and self._meta[slot, _S_END] == seq:
                return seq, Frame(image, timestamp)

    def close(self) -> None:
        if self._shm is None:
            return
        del self._header, self._meta, self._times, self._pixels
        self._shm.close()
        self._shm = None


class SharedFrameSource:
    """``FrameSource`` over a device host's shared frames (what the engine opens for a card)."""

    stale_after_s = STALE_AFTER_S

    def __init__(self, name: str, device_name: str = "", fps: float | None = None) -> None:
        self.name = name
        self._info = SourceInfo(SourceKind.VIDEO_DEVICE, device_name or name, BACKEND, fps)
        self._reader: SharedFrameReader | None = None
        self._last: tuple[int, Frame] | None = None

    @property
    def info(self) -> SourceInfo:
        return self._info

    def screen_rect(self) -> Rect | None:
        return None

    def open(self) -> None:
        if self._reader is None:
            self._reader = SharedFrameReader(self.name)
            self._last = None

    def close(self) -> None:
        if self._reader is not None:
            self._reader.close()
            self._reader = None

    def latest_frame(self) -> Frame | None:
        reader = self._reader
        if reader is None:
            return None
        if reader.closed:
            raise SourceLostError(f"{self._info.name} was closed")
        if self._last is None or reader.latest_seq != self._last[0]:
            self._last = reader.read()
        if self._last is None:
            return None
        frame = self._last[1]
        if time.monotonic() - frame.timestamp > self.stale_after_s:
            raise SourceLostError(f"{self._info.name} stopped delivering frames")
        return frame

    def wait_for_frame(self, newer_than: float, timeout: float) -> Frame | None:
        deadline = time.monotonic() + timeout
        while True:
            frame = self.latest_frame()
            if frame is not None and frame.timestamp > newer_than:
                return frame
            if time.monotonic() >= deadline:
                return None
            time.sleep(_POLL_S)

    def __enter__(self) -> SharedFrameSource:
        self.open()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
