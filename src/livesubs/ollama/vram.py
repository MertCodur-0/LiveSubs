"""Will a model fit in the graphics card? An estimate from the model's own metadata.

Memory = weights (the download size) + KV cache + a fixed overhead (compute graph, buffers).
The KV cache is computed per layer from ``/api/show``'s ``model_info``: KV heads x (key + value
length) x 2 bytes (f16, Ollama's default) x cached tokens, where sliding-window layers (Gemma)
only hold their window plus one batch. Calibrated against nvidia-smi on the dev machine (see
CLAUDE.md, step 12): qwen3:8b used 5.42 GB (estimate 5.83), translategemma 3.80 GB (3.82).

The budget is the card's memory minus what Windows and other apps hold (~1.2 GB measured with
no model loaded) and what the active profile keeps free for the game. A MoE
model over budget is a case of its own: Ollama keeps the experts in RAM and the model stays fast
(gemma4:26b-a4b ran at ~1.3 s per line with 10 GB of experts in RAM), while a dense model that
spills over slows down layer by layer.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

GB = 1_000_000_000
KV_BYTES = 2  # f16
BATCH_TOKENS = 512  # Ollama's num_batch: a sliding-window cache holds window + one batch
# Compute graph and runtime buffers on top of weights and KV cache.
OVERHEAD_BYTES = 300_000_000
# VRAM held by Windows, the browser and other apps on a PC that only runs LiveSubs.
DESKTOP_VRAM_BYTES = 1_000_000_000
FITS_SHARE = 0.9  # up to 90 % of the budget fits; the rest of the budget is "borderline"
# Gemma 3 has no sliding-window pattern in its metadata: every 6th layer is global.
_GEMMA3_GLOBAL_EVERY = 6


class Fit(StrEnum):
    FITS = "fits"
    BORDERLINE = "borderline"
    TOO_BIG = "too_big"  # dense model: the part that does not fit runs on the CPU, slowly
    MOE_SPLIT = "moe_split"  # MoE model: experts in RAM, attention on the GPU (fine)
    UNKNOWN = "unknown"  # no metadata or no graphics card found


@dataclass(frozen=True, slots=True)
class LayerCache:
    kv_heads: int
    key_length: int
    value_length: int
    window: int | None  # sliding-window layer: tokens it attends to; None = full context


@dataclass(frozen=True, slots=True)
class ModelArch:
    architecture: str
    layers: tuple[LayerCache, ...]
    expert_count: int = 0

    @property
    def is_moe(self) -> bool:
        return self.expert_count > 1


def _per_layer(value: Any, count: int) -> list[int]:
    if isinstance(value, list):
        return [int(v) for v in value[:count]] + [int(value[-1])] * (count - len(value))
    return [int(value)] * count


def parse_arch(model_info: Mapping[str, Any]) -> ModelArch | None:
    """The cache-relevant shape of a model from ``/api/show``'s ``model_info``; None if unknown."""
    arch = model_info.get("general.architecture")
    if not isinstance(arch, str):
        return None

    def get(key: str, default: Any = None) -> Any:
        return model_info.get(f"{arch}.{key}", default)

    try:
        count = int(get("block_count"))
        heads = _per_layer(get("attention.head_count"), count)
        kv_heads = _per_layer(get("attention.head_count_kv", get("attention.head_count")), count)
        embedding = int(get("embedding_length"))
    except (TypeError, ValueError, IndexError):
        return None
    head_dim = [embedding // h if h else 0 for h in heads]
    key_length = get("attention.key_length")
    value_length = get("attention.value_length")
    window = get("attention.sliding_window")
    pattern = get("attention.sliding_window_pattern")
    if isinstance(pattern, list):
        sliding = [bool(p) for p in _per_layer(pattern, count)]
    elif window and arch == "gemma3":
        sliding = [(i + 1) % _GEMMA3_GLOBAL_EVERY != 0 for i in range(count)]
    else:
        sliding = [False] * count  # unknown: assume full context (errs on the safe side)

    layers = []
    for i in range(count):
        swa = bool(window) and sliding[i]
        key = get("attention.key_length_swa", key_length) if swa else key_length
        value = get("attention.value_length_swa", value_length) if swa else value_length
        layers.append(
            LayerCache(
                kv_heads=kv_heads[i],
                key_length=int(key) if key else head_dim[i],
                value_length=int(value) if value else head_dim[i],
                window=int(window) if swa else None,
            )
        )
    return ModelArch(arch, tuple(layers), int(get("expert_count", 0) or 0))


def kv_cache_bytes(arch: ModelArch, num_ctx: int) -> int:
    total = 0
    for layer in arch.layers:
        tokens = num_ctx if layer.window is None else min(num_ctx, layer.window + BATCH_TOKENS)
        total += tokens * layer.kv_heads * (layer.key_length + layer.value_length) * KV_BYTES
    return total


def estimate_bytes(weights_bytes: int, arch: ModelArch | None, num_ctx: int) -> int:
    """Memory the loaded model needs, wherever it ends up (GPU, or GPU and RAM)."""
    kv = kv_cache_bytes(arch, num_ctx) if arch is not None else 0
    return weights_bytes + kv + OVERHEAD_BYTES


def budget_bytes(total_vram_bytes: int, game_reserve_gb: float) -> int:
    """VRAM left for the model: the card minus the desktop's share and the game's reserve."""
    return max(0, total_vram_bytes - DESKTOP_VRAM_BYTES - round(game_reserve_gb * GB))


def judge(estimate: int, budget: int, *, moe: bool) -> Fit:
    if budget <= 0:
        return Fit.UNKNOWN
    if estimate <= budget * FITS_SHARE:
        return Fit.FITS
    if estimate <= budget:
        return Fit.BORDERLINE
    return Fit.MOE_SPLIT if moe else Fit.TOO_BIG


def gpu_share(size: int, size_vram: int) -> float:
    """Part of a loaded model that sits in VRAM (``/api/ps``); 1.0 = fully on the GPU."""
    return min(1.0, size_vram / size) if size else 0.0
