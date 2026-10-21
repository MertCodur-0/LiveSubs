"""Measure the pipeline's latency stage by stage on a real source.

Stages so far: capture (frame delivery), preprocessing and change detection. Later steps add OCR
and translation. WGC only delivers frames when the content changes, so run it against something
that moves (a game, a video) to measure the delivery rate.

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
import time

import numpy as np

from livesubs.core.geometry import NormRect
from livesubs.core.profiles import MonitorSource, Source, VisionSettings, WindowSource
from livesubs.engine.capture import CaptureError, create_source, is_black_frame
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
    parser.add_argument("--outline", action="store_true", help="enable outline support")
    args = parser.parse_args()

    win32.enable_per_monitor_dpi_awareness()
    spec = resolve_source(args)
    source = create_source(spec, max_fps=args.max_fps, backend=args.backend)
    print(f"Source: {source.info.kind} {source.info.name!r} via {source.info.backend}")

    vision = VisionSettings(outline_support=args.outline)
    detector = ChangeDetector()
    delivery_ms: list[float] = []
    preprocess_ms: list[float] = []
    change_ms: list[float] = []
    changes: dict[Change, int] = dict.fromkeys(Change, 0)
    black_frames = 0
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
            changes[detector.update(result.mask)] += 1
            t2 = time.perf_counter()
            preprocess_ms.append((t1 - t0) * 1000)
            change_ms.append((t2 - t1) * 1000)
            black_frames += is_black_frame(frame.image)
    except CaptureError as exc:
        sys.exit(f"Capture failed: {type(exc).__name__}: {exc}")
    finally:
        source.close()

    print(f"\nOpen -> first frame: {open_ms:.1f} ms")
    rate = len(delivery_ms) / args.seconds
    print(f"Frames received: {len(delivery_ms)} ({rate:.1f}/s, cap {args.max_fps:g}/s)")
    print(f"Region {region.shape[1]}x{region.shape[0]} px; black frames: {black_frames}")
    print(summarize("present -> consumer", delivery_ms))
    print(summarize("preprocess", preprocess_ms))
    print(summarize("change detection", change_ms))
    print("  changes: " + ", ".join(f"{kind} {n}" for kind, n in changes.items() if n))
    print("  (WGC stamps the composition time, which can be a fraction of a ms ahead of 'now'.)")
    if rate < args.max_fps / 2:
        print("Note: WGC sends frames only on change; a static screen gives few frames.")


if __name__ == "__main__":
    main()
