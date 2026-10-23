"""Translation models LiveSubs recommends, and which one suits this PC.

Measured on the developer's PC (RTX 4070 Laptop 8 GB, 32 GB RAM), see CLAUDE.md: gemma4:26b-a4b
translates closest to natural Turkish (~1.3 s per line); it keeps its experts in RAM, so it needs
a 32 GB machine. gemma4:12b is nearly as fast and needs far less memory.

gemma4:26b ships a vision encoder translation never uses; its text-only copy (same weights file,
see ``client.create_text_only``) leaves that VRAM to the experts and translates ~12 % faster.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CatalogModel:
    name: str
    download_bytes: int
    min_ram_gb: float  # total RAM the model needs next to Windows and LiveSubs
    description_key: str  # i18n key
    text_only: str = ""  # name of the copy without the vision encoder, made after the download

    @property
    def use_name(self) -> str:
        """The model LiveSubs translates with once this one is installed."""
        return self.text_only or self.name


RECOMMENDED: tuple[CatalogModel, ...] = (
    CatalogModel(
        "gemma4:26b-a4b-it-qat",
        15_634_199_946,
        24.0,
        "catalog.gemma4_26b",
        text_only="gemma4-text:26b-a4b-it-qat",
    ),
    CatalogModel("gemma4:12b-it-qat", 7_151_003_754, 12.0, "catalog.gemma4_12b"),
)


def recommend(ram_gb: float) -> CatalogModel:
    """The best recommended model this much RAM can run, else the smallest one."""
    return next((m for m in RECOMMENDED if ram_gb >= m.min_ram_gb), RECOMMENDED[-1])


def entry_for(name: str) -> CatalogModel | None:
    """The recommended model ``name`` is, or was made from."""
    return next(
        (m for m in RECOMMENDED if same_model(name, m.name) or same_model(name, m.use_name)), None
    )


def same_model(a: str, b: str) -> bool:
    """Ollama names without a tag mean ":latest"."""
    return _full(a) == _full(b)


def _full(name: str) -> str:
    return name if ":" in name else f"{name}:latest"
