"""Versioned JSON persistence for Pydantic models."""

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError


class SettingsModel(BaseModel):
    """Base for persisted settings.

    Edits made by the UI are validated like loaded files, and an unknown field is an error in
    code (it is a typo). ``read_model`` drops unknown fields instead, so a file written by a
    newer build still loads.
    """

    model_config = ConfigDict(validate_assignment=True, extra="forbid")


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
        unknown = [e["loc"] for e in exc.errors() if e["type"] == "extra_forbidden"]
        if not unknown:
            raise StorageError(f"{path.name}: {exc}") from exc
    for loc in unknown:
        _delete(data, loc)
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise StorageError(f"{path.name}: {exc}") from exc


def _delete(data: object, loc: tuple[str | int, ...]) -> None:
    """Remove the value at a validation-error location."""
    node = data
    for part in loc[:-1]:
        node = _child(node, part)
    if isinstance(node, dict):
        node.pop(loc[-1], None)


def _child(node: object, part: str | int) -> object:
    """Step into ``node``; parts that are not keys (a discriminated union's tag) stay put."""
    if isinstance(node, dict) and part in node:
        return node[part]
    if isinstance(node, list) and isinstance(part, int) and part < len(node):
        return node[part]
    return node


def write_model(path: Path, model: BaseModel) -> None:
    """Write atomically: a crash mid-write never leaves a truncated file behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(model.model_dump_json(indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
