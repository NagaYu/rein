"""Field-level diff between two normalized snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .client import ENTITY_TYPES
from .snapshot import VOLATILE_FIELDS, Snapshot

ENTITY_FIELD = "*"  # field name used for whole-entity created/deleted rows


@dataclass(frozen=True)
class FieldChange:
    entity_type: str
    entity_id: str
    field: str
    change_type: str  # modified | added | removed | entity_created | entity_deleted
    before: Any
    after: Any
    entity_name: str | None = None
    entity_updated_time: str | None = None


def flatten(data: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Nested dicts become dotted paths; lists and scalars are leaves."""
    out: dict[str, Any] = {}
    for key, value in data.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict) and value:
            out.update(flatten(value, f"{path}."))
        else:
            out[path] = value
    return out


def _diffable(entity: dict[str, Any]) -> dict[str, Any]:
    return flatten({k: v for k, v in entity.items() if k not in VOLATILE_FIELDS})


def diff_entities(entity_type: str, before: dict[str, Any] | None, after: dict[str, Any] | None) -> list[FieldChange]:
    ref = after or before or {}
    entity_id, name = str(ref.get("id")), ref.get("name")
    updated = (after or {}).get("updated_time")
    if before is None and after is not None:
        return [FieldChange(entity_type, entity_id, ENTITY_FIELD, "entity_created", None, after, name, updated)]
    if after is None and before is not None:
        return [FieldChange(entity_type, entity_id, ENTITY_FIELD, "entity_deleted", before, None, name, None)]
    if before is None or after is None:
        return []
    b, a = _diffable(before), _diffable(after)
    changes = []
    for path in sorted(b.keys() | a.keys()):
        if path not in a:
            changes.append(FieldChange(entity_type, entity_id, path, "removed", b[path], None, name, updated))
        elif path not in b:
            changes.append(FieldChange(entity_type, entity_id, path, "added", None, a[path], name, updated))
        elif b[path] != a[path]:
            changes.append(FieldChange(entity_type, entity_id, path, "modified", b[path], a[path], name, updated))
    return changes


def diff_snapshots(before: Snapshot, after: Snapshot) -> list[FieldChange]:
    if before.account_id != after.account_id:
        raise ValueError(f"account mismatch: {before.account_id} vs {after.account_id}")
    changes: list[FieldChange] = []
    for entity_type in ENTITY_TYPES:
        b_all, a_all = before.entities.get(entity_type, {}), after.entities.get(entity_type, {})
        for entity_id in sorted(b_all.keys() | a_all.keys()):
            changes.extend(diff_entities(entity_type, b_all.get(entity_id), a_all.get(entity_id)))
    return changes
