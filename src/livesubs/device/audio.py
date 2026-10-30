"""The capture card's sound, played on a PC output while the game is watched on the preview.

The card shows up in Windows as a sound input of its own ("Dijital Ses Arabirimi (V-Z632)"),
found by the card's name in brackets. Input and output are two WASAPI streams with their own
clocks, joined by ``Ring``: it keeps ``TARGET_S`` of sound buffered (callback jitter), skips
back down to it when the clocks drift apart, and plays silence while it refills after an
underrun. PortAudio is initialised again before every start, so a device plugged in since (or a
changed Windows default output) is seen.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from enum import StrEnum
from typing import Any, Protocol

import numpy as np

log = logging.getLogger(__name__)

TARGET_S = 0.03  # buffered between the card and the output
MAX_S = 0.12  # more than this (clock drift, a stalled output): skip back to TARGET_S
STREAM_LATENCY_S = 0.02  # asked of WASAPI per stream; "low" (3 ms) glitches under load


class AudioErrorKind(StrEnum):
    NO_INPUT = "no_input"  # the card's sound input is not in Windows' list
    NO_OUTPUT = "no_output"  # the chosen output is not connected
    FAILED = "failed"  # a stream would not open


class AudioError(Exception):
    def __init__(self, kind: AudioErrorKind, message: str) -> None:
        super().__init__(message)
        self.kind = kind


class Ring:
    """Thread-safe sample buffer between the input and the output callback."""

    def __init__(self, rate: int, channels: int) -> None:
        self.target = int(rate * TARGET_S)
        self.limit = int(rate * MAX_S)
        self._buf = np.zeros((self.limit * 2, channels), np.float32)
        self._start = 0  # read position
        self._fill = 0
        self._priming = True
        self._lock = threading.Lock()
        self.underruns = 0
        self.skips = 0

    @property
    def fill(self) -> int:
        return self._fill

    def write(self, block: np.ndarray) -> None:
        size = len(self._buf)
        with self._lock:
            block = block[-size:]
            end = (self._start + self._fill) % size
            first = min(len(block), size - end)
            self._buf[end : end + first] = block[:first]
            self._buf[: len(block) - first] = block[first:]
            self._fill += len(block)
            if self._fill > self.limit:
                skip = self._fill - self.target
                self._start = (self._start + skip) % size
                self._fill = self.target
                self.skips += 1

    def read(self, out: np.ndarray) -> None:
        """Fills ``out`` (silence where there is nothing to play)."""
        size = len(self._buf)
        with self._lock:
            if self._priming:
                if self._fill < self.target:
                    out.fill(0)
                    return
                self._priming = False
            count = min(len(out), self._fill)
            first = min(count, size - self._start)
            out[:first] = self._buf[self._start : self._start + first]
            out[first:count] = self._buf[: count - first]
            out[count:] = 0
            self._start = (self._start + count) % size
            self._fill -= count
            if count < len(out):
                self._priming = True
                self.underruns += 1


def _sd() -> Any:
    import sounddevice

    return sounddevice


def _wasapi_devices(sd: Any) -> list[tuple[int, dict]]:
    apis = [i for i, api in enumerate(sd.query_hostapis()) if "WASAPI" in api["name"]]
    return [(i, d) for i, d in enumerate(sd.query_devices()) if d["hostapi"] in apis]


def list_outputs() -> list[str]:
    """Names of the connected sound outputs, as Windows shows them."""
    try:
        sd = _sd()
        sd._terminate()
        sd._initialize()
        return [d["name"] for _, d in _wasapi_devices(sd) if d["max_output_channels"] > 0]
    except Exception:
        log.exception("Cannot list sound outputs")
        return []


class Player(Protocol):
    def close(self) -> None: ...


class Passthrough:
    """Plays one card's sound on one output until ``close``."""

    def __init__(self, card_name: str, output: str) -> None:
        sd = _sd()
        sd._terminate()  # see the module docstring
        sd._initialize()
        devices = _wasapi_devices(sd)
        wanted = f"({card_name})".casefold()
        source = next(
            (
                (i, d)
                for i, d in devices
                if d["max_input_channels"] > 0
                and (wanted in d["name"].casefold() or d["name"].casefold() == card_name.casefold())
            ),
            None,
        )
        if source is None:
            raise AudioError(AudioErrorKind.NO_INPUT, f"no sound input for {card_name}")
        if output:
            target = next(
                (i for i, d in devices if d["max_output_channels"] > 0 and d["name"] == output),
                None,
            )
            if target is None:
                raise AudioError(AudioErrorKind.NO_OUTPUT, f"output {output!r} not connected")
        else:
            target = sd.query_hostapis(source[1]["hostapi"])["default_output_device"]
            if target < 0:
                raise AudioError(AudioErrorKind.NO_OUTPUT, "no default output")
        in_index, in_info = source
        rate = int(in_info["default_samplerate"])
        channels = min(in_info["max_input_channels"], 2)
        out_channels = min(sd.query_devices(target)["max_output_channels"], 2)
        self.ring = Ring(rate, channels)
        self._mono = out_channels < channels
        self._channels = channels
        try:
            self._input = sd.InputStream(
                device=in_index,
                samplerate=rate,
                channels=channels,
                dtype="float32",
                latency=STREAM_LATENCY_S,
                callback=self._on_input,
            )
            self._output = sd.OutputStream(
                device=target,
                samplerate=rate,
                channels=out_channels,
                dtype="float32",
                latency=STREAM_LATENCY_S,
                callback=self._on_output,
                extra_settings=sd.WasapiSettings(auto_convert=True),  # any mix format
            )
        except Exception as exc:
            self.close()
            raise AudioError(AudioErrorKind.FAILED, str(exc)) from exc
        self._input.start()
        self._output.start()
        log.info(
            "Playing %s on %s (%d Hz, %d ch)",
            in_info["name"],
            sd.query_devices(target)["name"],
            rate,
            channels,
        )

    def _on_input(self, data: np.ndarray, _frames: int, _time: Any, _status: Any) -> None:
        self.ring.write(data)

    def _on_output(self, out: np.ndarray, frames: int, _time: Any, _status: Any) -> None:
        if not self._mono:
            self.ring.read(out)
            return
        block = np.empty((frames, self._channels), np.float32)
        self.ring.read(block)
        out[:, 0] = block.mean(axis=1)

    def close(self) -> None:
        for name in ("_input", "_output"):
            stream = getattr(self, name, None)
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    log.exception("Closing the %s stream failed", name.strip("_"))
        if hasattr(self, "ring"):
            log.info("Sound stopped (underruns %d, skips %d)", self.ring.underruns, self.ring.skips)


PlayerFactory = Callable[[str, str], Player]


class AudioControl:
    """Plays the open card's sound while it is wanted and the preview is on screen.

    Called from the device thread (card opened/closed) and the Qt thread (setting, preview);
    ``emit`` gets ``(playing, error kind or None, detail)`` after every change.
    """

    def __init__(
        self,
        emit: Callable[[bool, AudioErrorKind | None, str], None],
        open_player: PlayerFactory = Passthrough,
    ) -> None:
        self._emit = emit
        self._open = open_player
        self._lock = threading.Lock()
        self._card: str | None = None
        self._output: str | None = None  # None: off; "" = Windows' default output
        self._preview = False
        self._playing: tuple[str, str] | None = None
        self._player: Player | None = None

    def set_card(self, name: str | None) -> None:
        self._update(card=name)

    def set_output(self, output: str | None) -> None:
        self._update(output=output)

    def set_preview(self, shown: bool) -> None:
        self._update(preview=shown)

    def close(self) -> None:
        self._update(card=None)

    def _update(self, **changes: Any) -> None:
        with self._lock:
            self._card = changes.get("card", self._card)
            self._output = changes.get("output", self._output)
            self._preview = changes.get("preview", self._preview)
            wanted = None
            if self._card and self._output is not None and self._preview:
                wanted = (self._card, self._output)
            if wanted == self._playing:
                return
            if self._player is not None:
                self._player.close()
                self._player = None
            self._playing = wanted
            if wanted is None:
                self._emit(False, None, "")
                return
            try:
                self._player = self._open(*wanted)
            except AudioError as exc:
                log.warning("No sound: %s", exc)
                self._emit(False, exc.kind, str(exc))
            except Exception as exc:
                log.exception("No sound")
                self._emit(False, AudioErrorKind.FAILED, str(exc))
            else:
                self._emit(True, None, "")
