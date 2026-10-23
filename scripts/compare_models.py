"""Translate subtitle lines with several Ollama models, side by side.

Lines come from tests/samples/samples.json (expected English next to the human reference; no OCR
involved) or, with ``--pack``, from a translation pack's ``test_lines``. With a pack every line
is translated twice: with the pack's game context, glossary and examples, and without them (the
built-in prompt), so the report shows what the pack changes. Lines that are also pack examples
are skipped: a model shown the answer proves nothing. Each model is loaded first; its load time,
VRAM fit (``/api/ps``: size_vram vs size) and the prompt's token count are reported, then each
line with its latency. As in live translation, the previous lines go along as context.

Example:
    .venv\\Scripts\\python.exe scripts\\compare_models.py --models qwen3:8b,gemma4:12b-it-qat
    .venv\\Scripts\\python.exe scripts\\compare_models.py --pack gta6.json --limit 60 --html r.html
"""

from __future__ import annotations

import argparse
import html
import json
import random
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx

from livesubs.core.translation_pack import TranslationPack, load_pack
from livesubs.engine.translate.base import (
    CancelToken,
    ContextLine,
    TranslationError,
    TranslationRequest,
)
from livesubs.engine.translate.ollama import DEFAULT_URL, OllamaBackend
from livesubs.engine.translate.prompt import build_messages

SAMPLES = Path(__file__).resolve().parent.parent / "tests" / "samples" / "samples.json"
PLAIN, WITH_PACK = "plain", "pack"


@dataclass
class Run:
    model: str
    variant: str
    outputs: list[tuple[str, float]]
    warm_ms: float
    memory: str
    prompt_tokens: int | None

    @property
    def label(self) -> str:
        return self.model if self.variant == PLAIN else f"{self.model} + pack"


def load_samples() -> list[tuple[str, str]]:
    spec = json.loads(SAMPLES.read_text(encoding="utf-8"))
    return [(s["expected"], s.get("reference_tr", "")) for s in spec["samples"] if s["expected"]]


def pack_lines(pack: TranslationPack, limit: int | None, seed: int) -> list[tuple[str, str]]:
    shown = set(pack.translation.examples)
    lines = [line for line in pack.test_lines if line not in shown]
    if limit and limit < len(lines):
        # A random but repeatable pick, kept in dialogue order so the context lines make sense.
        picked = set(random.Random(seed).sample(range(len(lines)), limit))
        lines = [line for i, line in enumerate(lines) if i in picked]
    return [(line, "") for line in lines]


def memory_report(url: str, model: str) -> str:
    running = httpx.get(f"{url}/api/ps", timeout=5).json().get("models", [])
    entry = next((m for m in running if m["name"] == model or m["model"] == model), None)
    if entry is None:
        return "not loaded"
    size, vram = entry["size"], entry.get("size_vram", 0)
    where = "fully on GPU" if vram >= size else f"{vram / size:.0%} on GPU (rest on CPU: slower)"
    return f"{size / 1e9:.1f} GB, {where}"


def prompt_tokens(url: str, model: str, request: TranslationRequest, num_ctx: int) -> int | None:
    """Tokens of a full prompt; Ollama silently cuts the start of a prompt longer than num_ctx."""
    body = {
        "model": model,
        "messages": build_messages(request),
        "stream": False,
        "think": False,
        "options": {"num_ctx": num_ctx, "num_predict": 1},
    }
    try:
        return httpx.post(f"{url}/api/chat", json=body, timeout=120).json().get("prompt_eval_count")
    except httpx.HTTPError:
        return None


def request_for(
    english: str, history: list[ContextLine], context_lines: int, pack: TranslationPack | None
) -> TranslationRequest:
    previous = tuple(history[-context_lines:]) if context_lines else ()
    if pack is None:
        return TranslationRequest(english, history=previous)
    hints = pack.translation
    return TranslationRequest(
        english,
        history=previous,
        game_context=hints.context,
        glossary=hints.glossary,
        examples=tuple(hints.examples.items()),
    )


def run_model(
    model: str,
    lines: list[tuple[str, str]],
    packs: dict[str, TranslationPack | None],
    args: argparse.Namespace,
) -> list[Run]:
    backend = OllamaBackend(model, url=args.url, num_ctx=args.num_ctx, keep_alive="10m")
    runs = []
    print(f"\n=== {model}")
    try:
        warm_ms = backend.warm_up()
        memory = memory_report(args.url, model)
        print(f"load + warm-up: {warm_ms / 1000:.1f} s; {memory}")
        for variant, pack in packs.items():
            history: list[ContextLine] = [ContextLine(lines[0][0], "…")] * args.context_lines
            tokens = prompt_tokens(
                args.url, model, request_for("Hi.", history, args.context_lines, pack), args.num_ctx
            )
            print(
                f"\n--- {variant}: prompt with {args.context_lines} context lines = {tokens} tokens"
            )
            if tokens is not None and tokens + 200 > args.num_ctx:
                print(f"    WARNING: prompt + answer exceed num_ctx {args.num_ctx}")
            history = []
            outputs = []
            for english, reference in lines:
                request = request_for(english, history, args.context_lines, pack)
                result = backend.translate(request, CancelToken())
                history.append(ContextLine(english, result.text))
                outputs.append((result.text, result.elapsed_ms))
                ref = f"\n  REF {reference}" if reference else ""
                print(f"\n  EN  {english}{ref}\n  OUT {result.text}  ({result.elapsed_ms:.0f} ms)")
            times = [ms for _, ms in outputs]
            print(
                f"\n{model} [{variant}]: median {statistics.median(times):.0f} ms, "
                f"p90 {sorted(times)[max(int(len(times) * 0.9) - 1, 0)]:.0f} ms, "
                f"max {max(times):.0f} ms"
            )
            runs.append(Run(model, variant, outputs, warm_ms, memory, tokens))
    except TranslationError as exc:
        print(f"failed: {type(exc).__name__}: {exc}", file=sys.stderr)
    finally:
        backend.unload()
        backend.close()
    return runs


def write_markdown(path: Path, lines: list[tuple[str, str]], runs: list[Run]) -> None:
    header = "| English | Reference | " + " | ".join(r.label for r in runs) + " |"
    rule = "|" + "---|" * (2 + len(runs))
    rows = []
    for i, (english, reference) in enumerate(lines):
        cells = [f"{r.outputs[i][0]} ({r.outputs[i][1]:.0f} ms)" for r in runs]
        rows.append("| " + " | ".join([english, reference, *cells]) + " |")
    path.write_text("\n".join([header, rule, *rows]) + "\n", encoding="utf-8")


def write_html(path: Path, lines: list[tuple[str, str]], runs: list[Run]) -> None:
    def cell(text: str, cls: str = "") -> str:
        return f'<td class="{cls}">{html.escape(text)}</td>'

    head = "".join(
        f"<th>{html.escape(r.label)}<small>median "
        f"{statistics.median(ms for _, ms in r.outputs):.0f} ms · load "
        f"{r.warm_ms / 1000:.1f} s · {r.prompt_tokens} prompt tokens<br>{html.escape(r.memory)}"
        "</small></th>"
        for r in runs
    )
    has_reference = any(reference for _, reference in lines)
    body = []
    for i, (english, reference) in enumerate(lines):
        cells = [cell(english, "en")]
        if has_reference:
            cells.append(cell(reference, "ref"))
        cells += [cell(r.outputs[i][0], r.variant) for r in runs]
        body.append(f"<tr><td class='n'>{i + 1}</td>{''.join(cells)}</tr>")
    reference_head = "<th>Reference</th>" if has_reference else ""
    path.write_text(
        f"""<!doctype html><meta charset="utf-8"><title>Model comparison</title>
<style>
body {{ font: 14px/1.4 system-ui, sans-serif; margin: 16px; background: #fff; color: #111; }}
table {{ border-collapse: collapse; width: 100%; }}
th, td {{ border: 1px solid #ccc; padding: 6px 8px; vertical-align: top; text-align: left; }}
th {{ position: sticky; top: 0; background: #f2f2f2; }}
th small {{ display: block; font-weight: normal; color: #555; }}
td.n {{ color: #888; }} td.en {{ font-weight: 600; }} td.ref {{ color: #075; }}
td.pack {{ background: #f6f9ff; }}
</style>
<table><tr><th>#</th><th>English</th>{reference_head}{head}</tr>
{chr(10).join(body)}
</table>
""",
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--models", default="qwen3:8b", help="comma-separated Ollama models")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--num-ctx", type=int, default=2048)
    parser.add_argument("--context-lines", type=int, default=3, help="previous lines as context")
    parser.add_argument("--pack", type=Path, help="translation pack: its test_lines and hints")
    parser.add_argument("--only-pack", action="store_true", help="skip the run without the pack")
    parser.add_argument("--limit", type=int, help="pack lines to use (random, repeatable)")
    parser.add_argument("--seed", type=int, default=6)
    parser.add_argument("--markdown", type=Path, help="also write a Markdown table here")
    parser.add_argument("--html", type=Path, help="also write an HTML table here")
    args = parser.parse_args()

    packs: dict[str, TranslationPack | None] = {PLAIN: None}
    if args.pack:
        pack = load_pack(args.pack)
        lines = pack_lines(pack, args.limit, args.seed)
        packs = {WITH_PACK: pack} if args.only_pack else {PLAIN: None, WITH_PACK: pack}
    else:
        lines = load_samples()
    print(f"{len(lines)} lines")

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    runs = [run for model in models for run in run_model(model, lines, packs, args)]
    if args.markdown and runs:
        write_markdown(args.markdown, lines, runs)
        print(f"\nWrote {args.markdown}")
    if args.html and runs:
        write_html(args.html, lines, runs)
        print(f"Wrote {args.html}")


if __name__ == "__main__":
    main()
