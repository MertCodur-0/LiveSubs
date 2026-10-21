"""Versioned JSON persistence for Pydantic models."""

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError


class SettingsModel(BaseModel):
    """Base for persisted settings: edits made by the UI are validated like loaded files."""

    model_config = ConfigDict(validate_assignment=True)


class StorageError(ValueError):
    """A settings file exists but cannot be used (corrupt, invalid or from a newer version)."""


def read_model[M: BaseModel](path: Path, model: type[M], schema_version: int) -> M:
    """Load and validate ``path``.

    Raises ``FileNotFoundError`` when it does not exist and ``StorageError`` when it cannot be
    used. Unknown fields are ignored, so files written by a newer build with the same schema
    version still load.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StorageError(f"{path.name}: not valid JSON ({exc})") from exc
    if not isinstance(data, dict):
        raise StorageError(f"{path.name}: expected a JSON object")

    version = data.get("schema_version", 1)
    if not isinstance(version, int) or version > schema_version:
        raise StorageError(f"{path.name}: unsupported schema version {version!r}")
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise StorageError(f"{path.name}: {exc}") from exc


def write_model(path: Path, model: BaseModel) -> None:
    """Write atomically: a crash mid-write never leaves a truncated file behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(model.model_dump_json(indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
