from __future__ import annotations

from datetime import datetime, timezone

import pytest

from rein.attribution import AttributionConfig, attribute, event_types_for
from rein.client import MockAdsClient
from rein.diff import FieldChange, diff_entities, diff_snapshots
from rein.ledger import Ledger
from rein.snapshot import Snapshot, normalize_entities, take_snapshot

from conftest import SCENARIOS

T0 = datetime(2026, 10, 27, 0, 0, tzinfo=timezone.utc)
T1 = datetime(2026, 10, 28, 0, 0, tzinfo=timezone.utc)


def snap(raw, t):
    return Snapshot("act_1", t, "v26.0", "test", normalize_entities(raw))


# ------------------------------------------------------------------ diff


def test_no_changes_for_reordered_and_retyped_fields():
    a = snap({"adset": [{"id": "1", "name": "x", "daily_budget": "5000", "targeting": {"age_min": 25, "age_max": 44}}]}, T0)
    b = snap({"adset": [{"targeting": {"age_max": 44, "age_min": 25}, "daily_budget": 5000, "name": "x", "id": 1}]}, T1)
    assert diff_snapshots(a, b) == []


def test_field_added_removed_modified():
    changes = diff_entities("adset", {"id": "1", "name": "a", "end_time": "x"}, {"id": "1", "name": "b", "bid_amount": 10})
    got = {(c.field, c.change_type, c.before, c.after) for c in changes}
    assert got == {("name", "modified", "a", "b"), ("end_time", "removed", "x", None), ("bid_amount", "added", None, 10)}


def test_nested_fields_are_dotted_paths():
    changes = diff_entities("adset", {"id": "1", "targeting": {"age_max": 44}}, {"id": "1", "targeting": {"age_max": 54}})
    assert [(c.field, c.before, c.after) for c in changes] == [("targeting.age_max", 44, 54)]


def test_volatile_fields_ignored():
    changes = diff_entities("ad", {"id": "1", "updated_time": "a", "effective_status": "ACTIVE"},
                            {"id": "1", "updated_time": "b", "effective_status": "PAUSED"})
    assert changes == []


def test_entity_created_and_deleted():
    a = snap({"ad": [{"id": "1", "name": "gone"}]}, T0)
    b = snap({"ad": [{"id": "2", "name": "new"}]}, T1)
    got = [(c.entity_id, c.change_type, c.field) for c in diff_snapshots(a, b)]
    assert got == [("1", "entity_deleted", "*"), ("2", "entity_created", "*")]


def test_account_mismatch_rejected():
    a = snap({}, T0)
    b = Snapshot("act_2", T1, "v26.0", "test", normalize_entities({}))
    with pytest.raises(ValueError):
        diff_snapshots(a, b)


def test_creative_swap_surfaces_as_resolved_link_change_on_ad():
    before = take_snapshot(MockAdsClient(SCENARIOS / "2_url_swap" / "before"))
    after = take_snapshot(MockAdsClient(SCENARIOS / "2_url_swap" / "after"))
    fields = {(c.entity_type, c.entity_id, c.field, c.change_type) for c in diff_snapshots(before, after)}
    assert ("ad", "120000000000000302", "creative.id", "modified") in fields
    assert ("ad", "120000000000000302", "creative_resolved.link", "modified") in fields
    assert ("creative", "120000000000000405", "*", "entity_created") in fields


# ------------------------------------------------------------------ attribution

CHANGE = FieldChange("adset", "201", "daily_budget", "modified", 5000, 50000)
CONFIG = AttributionConfig(
    applications={"app-ext": {"kind": "external_agent", "label": "ext"}, "app-ui": {"kind": "human", "label": "ui"}},
    actors={"actor-meta": {"kind": "meta_ai", "label": "meta"}},
)


def act(**kw):
    base = {"event_type": "update_ad_set_budget", "object_id": "201", "event_time": "2026-10-27T05:00:00+0000",
            "actor_id": "u1", "application_id": "app-ext"}
    base.update(kw)
    return base


def test_match_assigns_declared_kind_with_evidence():
    a = attribute(CHANGE, [act()], T0, T1, CONFIG)
    assert a.actor_kind == "external_agent"
    assert a.basis == "activity_match"
    assert a.evidence["activities"][0]["event_type"] == "update_ad_set_budget"
    assert a.evidence["mapping"] == ["application:app-ext"]


def test_actor_mapping_takes_precedence_over_application():
    a = attribute(CHANGE, [act(actor_id="actor-meta")], T0, T1, CONFIG)
    assert a.actor_kind == "meta_ai"


def test_no_activity_is_unknown():
    a = attribute(CHANGE, [], T0, T1, CONFIG)
    assert (a.actor_kind, a.basis) == ("unknown", "no_matching_activity")


@pytest.mark.parametrize("activity", [
    act(object_id="999"),                                  # other object
    act(event_type="update_ad_set_name"),                  # unrelated event
    act(event_time="2026-10-26T23:59:59Z"),                # before window
    act(event_time="2026-10-28T00:00:01Z"),                # after window
])
def test_non_matching_activity_is_unknown(activity):
    assert attribute(CHANGE, [activity], T0, T1, CONFIG).actor_kind == "unknown"


def test_window_end_is_inclusive_start_exclusive():
    assert attribute(CHANGE, [act(event_time="2026-10-28T00:00:00Z")], T0, T1, CONFIG).actor_kind == "external_agent"
    assert attribute(CHANGE, [act(event_time="2026-10-27T00:00:00Z")], T0, T1, CONFIG).actor_kind == "unknown"


def test_undeclared_actor_stays_unknown_but_keeps_evidence():
    a = attribute(CHANGE, [act(application_id="app-other")], T0, T1, CONFIG)
    assert (a.actor_kind, a.basis) == ("unknown", "activity_matched_actor_undeclared")
    assert a.evidence["activities"]


def test_conflicting_actors_are_ambiguous_unknown():
    a = attribute(CHANGE, [act(), act(application_id="app-ui")], T0, T1, CONFIG)
    assert (a.actor_kind, a.basis) == ("unknown", "ambiguous_activities")
    assert len(a.evidence["activities"]) == 2


def test_field_without_event_mapping_is_unknown():
    change = FieldChange("creative", "1", "*", "entity_created", None, {})
    assert event_types_for("creative", "*") == frozenset()
    assert attribute(change, [act(object_id="1")], T0, T1, CONFIG).basis == "no_event_type_mapping"


def test_nested_field_uses_top_level_mapping():
    assert "update_ad_creative" in event_types_for("ad", "creative_resolved.link")


# ------------------------------------------------------------------ ledger


def test_ledger_roundtrip_and_feed(tmp_path):
    ledger = Ledger(tmp_path / "rein.db")
    s0 = take_snapshot(MockAdsClient(SCENARIOS / "1_budget_spike" / "before"))
    s1 = take_snapshot(MockAdsClient(SCENARIOS / "1_budget_spike" / "after"))
    id0, id1 = ledger.save_snapshot(s0), ledger.save_snapshot(s1)
    assert ledger.previous_snapshot_id(s1.account_id, id1) == id0
    assert ledger.load_snapshot(id0).entities == s0.entities
    assert ledger.load_snapshot(id1).activities == s1.activities

    items = [(c, attribute(c, s1.activities, s0.taken_at, s1.taken_at, CONFIG)) for c in diff_snapshots(s0, s1)]
    ids = ledger.record_changes(s1.account_id, id0, id1, s1.taken_at, items)
    assert len(ids) == 1
    # Re-recording the same diff is idempotent.
    assert ledger.record_changes(s1.account_id, id0, id1, s1.taken_at, items) == []

    rec = ledger.get_change(ids[0])
    assert (rec.entity_type, rec.field, rec.before, rec.after) == ("adset", "daily_budget", 5000, 50000)
    assert rec.detected_at == s1.taken_at
    assert [r.id for r in ledger.feed()] == ids
    assert ledger.feed(flagged_only=True) == []
