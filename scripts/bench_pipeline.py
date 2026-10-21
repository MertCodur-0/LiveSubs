"""Measure the pipeline's latency stage by stage on a real source.

Stages: capture (frame delivery), preprocessing, change detection, OCR (when the stabilizer asks
for a read), confirmation and translation (Ollama, GPU lane). The headline number is
"appear -> translated": from the frame where a subtitle first showed up to its translation being
ready. WGC only delivers frames when the content changes, so run it against something that moves
(a game, a video with subtitles) to measure the delivery rate.

Examples:
    .venv\\Scripts\\python.exe scripts\\bench_pipeline.py                       # primary monitor
    .venv\\Scripts\\python.exe scripts\\bench_pipeline.py --monitor DISPLAY1
    .venv\\Scripts\\python.exe scripts\\bench_pipeline.py --window GTA5.exe --seconds 20
    .venv\\Scripts\\python.exe scripts\\bench_pipeline.py --backend mss
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from collections.abc import Callable

import cv2
import numpy as np

from livesubs.core.geometry import NormRect
from livesubs.core.profiles import MonitorSource, Source, VisionSettings, WindowSource
from livesubs.engine.capture import CaptureError, create_source, is_black_frame
from livesubs.engine.ocr.language import english_ocr_language
from livesubs.engine.ocr.winrt_ocr import WinRtOcr
from livesubs.engine.text.stabilizer import LineConfirmed, LineUpgraded, Stabilizer
from livesubs.engine.translate.base import TranslationError
from livesubs.engine.translate.cache import TranslationCache
from livesubs.engine.translate.ollama import OllamaBackend
from livesubs.engine.translate.orchestrator import (
    LineTranslated,
    TranslationEvent,
    TranslationOrchestrator,
)
from livesubs.engine.vision.change import Change, ChangeDetector
from livesubs.engine.vision.preprocess import preprocess
from livesubs.platform import win32


def parse_region(text: str) -> NormRect:
    x, y, width, height = (float(v) for v in text.split(","))
    return NormRect(x=x, y=y, width=width, height=height)


def resolve_source(args: argparse.Namespace) -> Source:
    if args.window:
        needle = args.window.lower()
        for window in win32.list_windows(exclude_pid=os.getpid()):
            if needle in window.exe_name.lower() or needle in window.title.lower():
                return WindowSource(exe_name=window.exe_name, title=window.title)
        sys.exit(f"No window matches {args.window!r}.")
    monitors = win32.list_monitors()
    if args.monitor:
        wanted = args.monitor.upper().rsplit("\\", 1)[-1]  # "DISPLAY1" or r"\\.\DISPLAY1"
        for monitor in monitors:
            if monitor.device_name.upper().endswith("\\" + wanted):
                return MonitorSource(device_name=monitor.device_name)
        names = ", ".join(m.device_name for m in monitors)
        sys.exit(f"Unknown monitor {args.monitor!r}. Connected: {names}")
    return MonitorSource(device_name=next(m for m in monitors if m.is_primary).device_name)


class LineTimes:
    """Per subtitle line: when it appeared, was confirmed, and translated (thread-safe)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._lines: dict[int, dict[str, object]] = {}

    def confirmed(self, line_id: int, text: str, appeared_at: float) -> None:
        with self._lock:
            self._lines[line_id] = {
                "text": text,
                "appeared": appeared_at,
                "confirmed": time.monotonic(),
            }

    def on_event(self, event: TranslationEvent) -> None:
        with self._lock:
            line = self._lines.get(event.line_id)
            if line is None:
                return
            if isinstance(event, LineTranslated):
                line["translation"] = event.result.text  # an upgrade replaces the text...
                if "translated" not in line:  # ...but the latency is the first translation's
                    line["translated"] = time.monotonic()
                    line["translate_ms"] = 0.0 if event.result.cached else event.result.elapsed_ms
            else:
                print(f"  translation failed: {event.error}")

    def _values(self, compute: Callable[[dict[str, object]], float | None]) -> list[float]:
        with self._lock:
            return [v for line in self._lines.values() if (v := compute(line)) is not None]

    def confirm_ms(self) -> list[float]:
        return self._values(lambda x: (x["confirmed"] - x["appeared"]) * 1000)

    def translate_ms(self) -> list[float]:
        return self._values(lambda x: x.get("translate_ms"))

    def end_to_end_ms(self) -> list[float]:
        return self._values(
            lambda x: (x["translated"] - x["appeared"]) * 1000 if "translated" in x else None
        )

    def translations(self) -> list[tuple[str, str]]:
        with self._lock:
            return [(x["text"], x.get("translation", "-")) for x in self._lines.values()]


def start_translation(model: str, lines: LineTimes) -> TranslationOrchestrator | None:
    backend = OllamaBackend(model)
    try:
        warm_ms = backend.warm_up()
        print(f"Model {model} ready in {warm_ms / 1000:.1f} s (load + warm-up)")
    except TranslationError as exc:
        print(f"Translation disabled ({type(exc).__name__}: {exc})")
        return None
    return TranslationOrchestrator(backend, TranslationCache(), lines.on_event)


def summarize(name: str, values_ms: list[float]) -> str:
    if not values_ms:
        return f"  {name:<24} no samples"
    values = np.asarray(values_ms)
    p50, p95 = np.percentile(values, [50, 95])
    return f"  {name:<24} p50 {p50:7.2f} ms   p95 {p95:7.2f} ms   max {values.max():7.2f} ms"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--monitor", help="monitor such as DISPLAY1 (default: primary)")
    target.add_argument("--window", help="substring of a window title or executable name")
    parser.add_argument("--backend", choices=("wgc", "mss"), default="wgc")
    parser.add_argument("--max-fps", type=float, default=30.0)
    parser.add_argument("--seconds", type=float, default=10.0)
    parser.add_argument(
        "--region",
        type=parse_region,
        default=NormRect(x=0.1, y=0.75, width=0.8, height=0.2),
        help="subtitle region x,y,w,h normalized to the frame (default 0.1,0.75,0.8,0.2)",
    )
    parser.add_argument("--colors", default="#FFFFFF", help="subtitle colors, comma-separated")
    parser.add_argument("--no-contrast", action="store_true", help="disable local contrast")
    parser.add_argument("--model", default="qwen3:8b", help="Ollama model ('' = no translation)")
    args = parser.parse_args()

    win32.enable_per_monitor_dpi_awareness()
    spec = resolve_source(args)
    source = create_source(spec, max_fps=args.max_fps, backend=args.backend)
    print(f"Source: {source.info.kind} {source.info.name!r} via {source.info.backend}")

    vision = VisionSettings(text_colors=args.colors.split(","), local_contrast=not args.no_contrast)
    detector = ChangeDetector()
    stabilizer = Stabilizer()
    ocr_language = english_ocr_language()
    ocr = WinRtOcr(ocr_language) if ocr_language else None
    if ocr is None:
        print("English OCR is not installed; skipping OCR and translation.")
    lines = LineTimes()
    orchestrator = start_translation(args.model, lines) if ocr and args.model else None

    delivery_ms: list[float] = []
    preprocess_ms: list[float] = []
    change_ms: list[float] = []
    ocr_ms: list[float] = []
    changes: dict[Change, int] = dict.fromkeys(Change, 0)
    black_frames = 0
    line_id = 0
    appeared_at = 0.0
    started = time.monotonic()
    try:
        source.open()
        first = source.wait_for_frame(0.0, timeout=5.0)
        if first is None:
            sys.exit("No frame within 5 s (minimized window? static content with mss?).")
        open_ms = (time.monotonic() - started) * 1000
        print(f"Frame {first.width}x{first.height}, screen rect {source.screen_rect()}")
        print(f"Measuring for {args.seconds:.0f} s...")

        last, deadline = first.timestamp, time.monotonic() + args.seconds
        while time.monotonic() < deadline:
            frame = source.wait_for_frame(last, timeout=0.5)
            if frame is None:
                continue
            received = time.monotonic()
            delivery_ms.append((received - frame.timestamp) * 1000)
            last = frame.timestamp

            t0 = time.perf_counter()
            r = args.region.to_pixels(frame.width, frame.height)
            region = frame.image[r.y : r.bottom, r.x : r.right]
            result = preprocess(region, vision)
            t1 = time.perf_counter()
            change = detector.update(result.mask)
            changes[change] += 1
            t2 = time.perf_counter()
            preprocess_ms.append((t1 - t0) * 1000)
            change_ms.append((t2 - t1) * 1000)
            black_frames += is_black_frame(frame.image)
            if ocr is None:
                continue
            if change in (Change.APPEARED, Change.CHANGED):
                appeared_at = frame.timestamp
            decision = stabilizer.update(change, cv2.countNonZero(result.mask), time.monotonic())
            if not decision.read:
                continue
            read = ocr.recognize(result.ocr_image)
            ocr_ms.append(read.elapsed_ms)
            event = stabilizer.on_read(read.text, time.monotonic())
            if isinstance(event, LineConfirmed) and not event.repeat:
                line_id += 1
                lines.confirmed(line_id, event.text, appeared_at)
                print(f"  confirmed: {event.text!r}")
            elif isinstance(event, LineUpgraded):
                print(f"  upgraded:  {event.text!r}")  # same line: timings stay the first ones
            else:
                continue
            if orchestrator is not None:
                orchestrator.submit(line_id, event.text)
    except CaptureError as exc:
        sys.exit(f"Capture failed: {type(exc).__name__}: {exc}")
    finally:
        source.close()
        if orchestrator is not None:
            time.sleep(min(2.0, args.seconds))  # let the last translation finish
            orchestrator.close()

    print(f"\nOpen -> first frame: {open_ms:.1f} ms")
    rate = len(delivery_ms) / args.seconds
    print(f"Frames received: {len(delivery_ms)} ({rate:.1f}/s, cap {args.max_fps:g}/s)")
    print(f"Region {region.shape[1]}x{region.shape[0]} px; black frames: {black_frames}")
    print(summarize("present -> consumer", delivery_ms))
    print(summarize("preprocess", preprocess_ms))
    print(summarize("change detection", change_ms))
    print("  changes: " + ", ".join(f"{kind} {n}" for kind, n in changes.items() if n))
    if ocr is not None:
        print(summarize("OCR (when asked)", ocr_ms))
        print(summarize("appear -> confirmed", lines.confirm_ms()))
    if orchestrator is not None:
        print(summarize("translation", lines.translate_ms()))
        print(summarize("appear -> translated", lines.end_to_end_ms()))
        for source_text, translation in lines.translations():
            print(f"  {source_text!r}\n    -> {translation!r}")
    print("  (WGC stamps the composition time, which can be a fraction of a ms ahead of 'now'.)")
    if rate < args.max_fps / 2:
        print("Note: WGC sends frames only on change; a static screen gives few frames.")


if __name__ == "__main__":
    main()
