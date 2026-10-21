"""Translate the sample subtitles with several Ollama models, next to the human reference.

Uses the expected English of every sample in tests/samples/samples.json (no OCR involved), so
it compares models only. Each model is loaded first; its load time and whether it fits in VRAM
(``/api/ps``: size_vram vs size) are reported, then one line at a time with its latency.

Example:
    .venv\\Scripts\\python.exe scripts\\compare_models.py --models qwen3:8b,gemma4:12b-it-qat
    .venv\\Scripts\\python.exe scripts\\compare_models.py --markdown report.md
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

import httpx

from livesubs.engine.translate.base import CancelToken, TranslationError, TranslationRequest
from livesubs.engine.translate.ollama import DEFAULT_URL, OllamaBackend

SAMPLES = Path(__file__).resolve().parent.parent / "tests" / "samples" / "samples.json"


def load_lines() -> list[tuple[str, str]]:
    spec = json.loads(SAMPLES.read_text(encoding="utf-8"))
    return [(s["expected"], s.get("reference_tr", "")) for s in spec["samples"] if s["expected"]]


def memory_report(url: str, model: str) -> str:
    running = httpx.get(f"{url}/api/ps", timeout=5).json().get("models", [])
    entry = next((m for m in running if m["name"] == model or m["model"] == model), None)
    if entry is None:
        return "not loaded"
    size, vram = entry["size"], entry.get("size_vram", 0)
    where = "fully on GPU" if vram >= size else f"{vram / size:.0%} on GPU (rest on CPU: slower)"
    return f"{size / 1e9:.1f} GB, {where}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--models", default="qwen3:8b", help="comma-separated Ollama models")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--num-ctx", type=int, default=2048)
    parser.add_argument("--markdown", type=Path, help="also write a Markdown table here")
    args = parser.parse_args()

    lines = load_lines()
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    outputs: dict[str, list[tuple[str, float]]] = {}
    for model in models:
        backend = OllamaBackend(model, url=args.url, num_ctx=args.num_ctx, keep_alive="10m")
        print(f"\n=== {model}")
        try:
            warm_ms = backend.warm_up()
            print(f"load + warm-up: {warm_ms / 1000:.1f} s; {memory_report(args.url, model)}")
            results = []
            for english, reference in lines:
                result = backend.translate(TranslationRequest(english), CancelToken())
                results.append((result.text, result.elapsed_ms))
                print(f"\n  EN  {english}\n  REF {reference}\n  OUT {result.text}")
                print(f"      {result.elapsed_ms:.0f} ms")
            times = [ms for _, ms in results]
            print(
                f"\n{model}: median {statistics.median(times):.0f} ms, "
                f"p90 {sorted(times)[int(len(times) * 0.9) - 1]:.0f} ms, max {max(times):.0f} ms"
            )
            outputs[model] = results
        except TranslationError as exc:
            print(f"failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        finally:
            backend.unload()
            backend.close()

    if args.markdown and outputs:
        header = "| English | Reference | " + " | ".join(outputs) + " |"
        rule = "|" + "---|" * (2 + len(outputs))
        rows = []
        for i, (english, reference) in enumerate(lines):
            cells = [f"{outputs[m][i][0]} ({outputs[m][i][1]:.0f} ms)" for m in outputs]
            rows.append("| " + " | ".join([english, reference, *cells]) + " |")
        args.markdown.write_text("\n".join([header, rule, *rows]) + "\n", encoding="utf-8")
        print(f"\nWrote {args.markdown}")


if __name__ == "__main__":
    main()
