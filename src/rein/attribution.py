"""Who made a change — only as far as the ad account activity log can tell.

Rules (no guessing):
1. A change is matched to activities on the same object_id, with an event_type known to
   correspond to that field, whose event_time falls in (previous snapshot, this snapshot].
2. A matched activity yields an actor kind only if its actor_id or application_id is
   explicitly declared in the policy's `attribution` section.
3. Anything else — no mapping for the field, no match, ambiguous matches, undeclared
   actor/application — is `unknown`, with the reason and any matched activity kept as evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .diff import FieldChange
from .timeutil import iso, parse_time

ACTOR_KINDS = ("human", "external_agent", "meta_ai", "unknown")

# event_type values from the Ad Activity reference that correspond to a field we diff.
# Legacy naming in that enum is inconsistent (e.g. update_campaign_* may refer to ad sets),
# but matching also requires object_id equality, so listing both is safe.
_FIELD_EVENTS: dict[tuple[str, str], frozenset[str]] = {
    ("campaign", "daily_budget"): frozenset({"update_campaign_budget"}),
    ("campaign", "lifetime_budget"): frozenset({"update_campaign_budget"}),
    ("campaign", "spend_cap"): frozenset({"update_campaign_group_spend_cap"}),
    ("campaign", "status"): frozenset({"update_campaign_run_status"}),
    ("campaign", "name"): frozenset({"update_campaign_name"}),
    ("campaign", "*"): frozenset({"create_campaign_group"}),
    ("adset", "daily_budget"): frozenset({"update_ad_set_budget"}),
    ("adset", "lifetime_budget"): frozenset({"update_ad_set_budget"}),
    ("adset", "status"): frozenset({"update_ad_set_run_status", "update_campaign_run_status"}),
    ("adset", "name"): frozenset({"update_ad_set_name", "update_campaign_name"}),
    ("adset", "bid_amount"): frozenset({"update_ad_set_bidding"}),
    ("adset", "bid_strategy"): frozenset({"update_ad_set_bid_strategy"}),
    ("adset", "optimization_goal"): frozenset({"update_ad_set_optimization_goal"}),
    ("adset", "start_time"): frozenset({"update_ad_set_duration"}),
    ("adset", "end_time"): frozenset({"update_ad_set_duration"}),
    ("adset", "targeting"): frozenset({"update_ad_set_target_spec"}),
    ("adset", "*"): frozenset({"create_ad_set", "create_campaign_legacy"}),
    ("ad", "status"): frozenset({"update_ad_run_status", "update_ad_run_status_to_be_set_after_review"}),
    ("ad", "name"): frozenset({"update_ad_friendly_name"}),
    ("ad", "creative"): frozenset({"update_ad_creative", "edit_and_update_ad_creative"}),
    ("ad", "creative_resolved"): frozenset({"update_ad_creative", "edit_and_update_ad_creative"}),
    ("ad", "*"): frozenset({"create_ad"}),
}


def event_types_for(entity_type: str, field_path: str) -> frozenset[str]:
    top = field_path.split(".", 1)[0]
    return _FIELD_EVENTS.get((entity_type, top), frozenset())


@dataclass
class AttributionConfig:
    """Explicit, advertiser-declared mapping from activity actors/applications to actor kinds."""

    applications: dict[str, dict[str, str]] = field(default_factory=dict)  # id -> {kind, label}
    actors: dict[str, dict[str, str]] = field(default_factory=dict)

    def classify(self, activity: dict[str, Any]) -> tuple[str | None, str | None]:
        actor = self.actors.get(str(activity.get("actor_id")))
        if actor:
            return actor["kind"], f"actor:{activity.get('actor_id')}"
        app = self.applications.get(str(activity.get("application_id")))
        if app:
            return app["kind"], f"application:{activity.get('application_id')}"
        return None, None


@dataclass
class Attribution:
    actor_kind: str
    evidence: dict[str, Any]

    @property
    def basis(self) -> str:
        return self.evidence.get("basis", "")


def activity_ref(activity: dict[str, Any]) -> dict[str, Any]:
    keys = ("event_type", "event_time", "object_id", "object_type", "actor_id", "actor_name",
            "application_id", "application_name")
    return {k: activity.get(k) for k in keys if activity.get(k) is not None}


def attribute(
    change: FieldChange,
    activities: list[dict[str, Any]],
    window_start: datetime | None,
    window_end: datetime,
    config: AttributionConfig | None = None,
) -> Attribution:
    config = config or AttributionConfig()
    window = {"start": iso(window_start) if window_start else None, "end": iso(window_end)}
    types = event_types_for(change.entity_type, change.field)
    if not types:
        return Attribution("unknown", {"basis": "no_event_type_mapping", "window": window})

    candidates = []
    for act in activities:
        if str(act.get("object_id")) != change.entity_id or act.get("event_type") not in types:
            continue
        try:
            t = parse_time(act["event_time"])
        except (KeyError, ValueError, TypeError):
            continue
        if (window_start is None or t > window_start) and t <= window_end:
            candidates.append(act)

    if not candidates:
        return Attribution("unknown", {"basis": "no_matching_activity", "window": window,
                                       "event_types": sorted(types)})

    classified = [(act, *config.classify(act)) for act in candidates]
    kinds = {kind for _, kind, _ in classified}
    refs = [activity_ref(a) for a, _, _ in classified]
    if len(kinds) > 1:
        return Attribution("unknown", {"basis": "ambiguous_activities", "window": window, "activities": refs})
    kind = kinds.pop()
    if kind is None:
        return Attribution("unknown", {"basis": "activity_matched_actor_undeclared", "window": window,
                                       "activities": refs})
    if kind not in ACTOR_KINDS:
        raise ValueError(f"invalid actor kind in attribution config: {kind}")
    return Attribution(kind, {"basis": "activity_match", "window": window, "activities": refs,
                              "mapping": sorted({m for _, _, m in classified})})
