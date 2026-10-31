"""Snapshot capture and normalization.

Normalization makes snapshots comparable regardless of field order, omitted/null fields and
type wobble (Graph returns budgets as numeric strings; recorded or hand-written data may use ints).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .client import ENTITY_TYPES, AdsReader
from .timeutil import iso

# Budget-like fields are compared as integers in the account currency's API unit.
NUMERIC_FIELDS = frozenset({"daily_budget", "lifetime_budget", "spend_cap", "bid_amount", "budget_remaining"})
ID_FIELDS = frozenset({"id", "campaign_id", "adset_id", "account_id", "page_id"})
# Stored in the snapshot but excluded from diffs: they move without anyone editing the entity.
VOLATILE_FIELDS = frozenset({"updated_time", "effective_status", "budget_remaining"})


@dataclass
class Snapshot:
    account_id: str
    taken_at: datetime
    api_version: str
    source: str
    entities: dict[str, dict[str, dict[str, Any]]]
    activities: list[dict[str, Any]] = field(default_factory=list)
    id: int | None = None

    def get(self, entity_type: str, entity_id: str) -> dict[str, Any] | None:
        return self.entities.get(entity_type, {}).get(str(entity_id))

    def counts(self) -> dict[str, int]:
        return {t: len(self.entities.get(t, {})) for t in ENTITY_TYPES}


def _to_int(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        s = value.strip()
        try:
            return int(s)
        except ValueError:
            try:
                f = float(s)
            except ValueError:
                return value
            return int(f) if f.is_integer() else value
    return value


def normalize_value(value: Any) -> Any:
    """Recursively drop None/empty values and sort dict keys."""
    if isinstance(value, dict):
        out = {}
        for key in sorted(value):
            v = normalize_value(value[key])
            if v is None or v == {} or v == "":
                continue
            out[key] = v
        return out
    if isinstance(value, list):
        return [normalize_value(v) for v in value]
    return value


def normalize_entity(entity_type: str, raw: dict[str, Any]) -> dict[str, Any]:
    data = dict(raw)
    for key in NUMERIC_FIELDS & data.keys():
        data[key] = _to_int(data[key])
    for key in ID_FIELDS & data.keys():
        if data[key] is not None:
            data[key] = str(data[key])
    if entity_type == "ad" and "creative" in data:
        creative = data["creative"]
        if isinstance(creative, (str, int)):
            creative = {"id": creative}
        if isinstance(creative, dict) and creative.get("id") is not None:
            data["creative"] = {"id": str(creative["id"])}
    if entity_type == "creative":
        data["resolved"] = resolve_creative_fields(data)
    return normalize_value(data)


def _first(*values: Any) -> Any:
    for v in values:
        if v not in (None, "", [], {}):
            return v
    return None


def resolve_creative_fields(creative: dict[str, Any]) -> dict[str, Any]:
    """Pull the user-facing link and text out of the various creative shapes."""
    spec = creative.get("object_story_spec") or {}
    link_data = spec.get("link_data") or {}
    video_data = spec.get("video_data") or {}
    feed = creative.get("asset_feed_spec") or {}
    feed_links = [u.get("website_url") for u in feed.get("link_urls", []) if u.get("website_url")]
    feed_bodies = [b.get("text") for b in feed.get("bodies", []) if b.get("text")]
    feed_titles = [t.get("text") for t in feed.get("titles", []) if t.get("text")]
    feed_descs = [d.get("text") for d in feed.get("descriptions", []) if d.get("text")]

    def one_or_list(items: list[str]) -> Any:
        return items[0] if len(items) == 1 else (items or None)

    cta = link_data.get("call_to_action") or video_data.get("call_to_action") or {}
    return normalize_value({
        "link": _first(
            link_data.get("link"),
            (cta.get("value") or {}).get("link"),
            creative.get("link_url"),
            one_or_list(feed_links),
        ),
        "message": _first(link_data.get("message"), video_data.get("message"), creative.get("body"), one_or_list(feed_bodies)),
        "headline": _first(link_data.get("name"), video_data.get("title"), creative.get("title"), one_or_list(feed_titles)),
        "description": _first(link_data.get("description"), video_data.get("link_description"), one_or_list(feed_descs)),
        "call_to_action": _first(cta.get("type"), creative.get("call_to_action_type")),
    })


def normalize_entities(raw: dict[str, list[dict[str, Any]]]) -> dict[str, dict[str, dict[str, Any]]]:
    entities: dict[str, dict[str, dict[str, Any]]] = {}
    for entity_type in ENTITY_TYPES:
        by_id: dict[str, dict[str, Any]] = {}
        for row in raw.get(entity_type, []):
            norm = normalize_entity(entity_type, row)
            by_id[norm["id"]] = norm
        entities[entity_type] = dict(sorted(by_id.items()))
    # Ads point at immutable creatives; expose the effective link/text on the ad itself so a
    # creative swap shows up as a change of `creative_resolved.link` etc. on the ad.
    for ad in entities["ad"].values():
        creative_id = (ad.get("creative") or {}).get("id")
        creative = entities["creative"].get(creative_id) if creative_id else None
        if creative and creative.get("resolved"):
            ad["creative_resolved"] = creative["resolved"]
    return entities


def take_snapshot(reader: AdsReader) -> Snapshot:
    raw = {t: reader.list_entities(t) for t in ENTITY_TYPES}
    activities = reader.list_activities()
    return Snapshot(
        account_id=reader.account_id(),
        taken_at=reader.observed_at(),
        api_version=reader.api_version,
        source=reader.source,
        entities=normalize_entities(raw),
        activities=activities,
    )


def to_json(snapshot: Snapshot) -> str:
    return json.dumps(
        {
            "account_id": snapshot.account_id,
            "taken_at": iso(snapshot.taken_at),
            "api_version": snapshot.api_version,
            "source": snapshot.source,
            "entities": snapshot.entities,
        },
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    )
