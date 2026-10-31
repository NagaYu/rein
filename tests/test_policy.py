from __future__ import annotations

import pytest

from rein.policy import PolicyError, evaluate, load_policy, parse_policy

from conftest import POLICY_EXAMPLE
from helpers import change


def policy(*rules, **top):
    return parse_policy({"version": 1, "timezone": "Asia/Tokyo", "rules": list(rules), **top})


CAP = {"id": "cap", "type": "budget_cap", "entity_types": ["adset"], "fields": ["daily_budget"], "max": 20000}


def test_example_policy_loads():
    p = load_policy(POLICY_EXAMPLE)
    assert {r.type for r in p.rules} == {"budget_cap", "frozen_fields", "prohibited_terms", "change_window"}
    assert p.attribution.applications["900000000000002"]["kind"] == "external_agent"


# ------------------------------------------------------------------ budget_cap


@pytest.mark.parametrize("after,flagged", [(19999, False), (20000, False), (20001, True), (0, False)])
def test_budget_cap_boundary(after, flagged):
    ev = evaluate(change(before=5000, after=after), policy(CAP))
    assert ev.violation is flagged


def test_budget_cap_ignores_other_levels_fields_and_entities():
    p = policy(dict(CAP, entity_ids=["201"]))
    assert not evaluate(change(entity_type="campaign", after=99999), p).findings
    assert not evaluate(change(field="lifetime_budget", after=99999), p).findings
    assert not evaluate(change(entity_id="999", after=99999), p).findings
    assert evaluate(change(entity_id="201", after=99999), p).violation


def test_budget_cap_on_created_entity_and_non_numeric():
    p = policy(CAP)
    assert evaluate(change(change_type="entity_created", field="*", after={"id": "201", "daily_budget": 30000}), p).violation
    assert not evaluate(change(change_type="entity_created", field="*", after={"id": "201"}), p).findings
    ev = evaluate(change(after="lots"), p)
    assert ev.violation and "数値でない" in ev.findings[0].reason


def test_budget_cap_removed_field_is_not_a_cap_breach():
    assert not evaluate(change(change_type="removed", before=5000, after=None), policy(CAP)).findings


# ------------------------------------------------------------------ frozen_fields

FROZEN = {"id": "frozen", "type": "frozen_fields", "entity_types": ["ad"], "fields": ["creative_resolved.link", "name"]}


@pytest.mark.parametrize("field,change_type,flagged", [
    ("creative_resolved.link", "modified", True),
    ("creative_resolved.link", "removed", True),
    ("creative_resolved.link", "added", True),
    ("creative_resolved.message", "modified", False),
    ("name", "modified", True),
    ("Name", "modified", False),  # field paths are case-sensitive
])
def test_frozen_fields(field, change_type, flagged):
    ev = evaluate(change(entity_type="ad", field=field, before="a", after="b", change_type=change_type), policy(FROZEN))
    assert ev.violation is flagged


def test_frozen_wildcard_and_deletion():
    p = policy(dict(FROZEN, fields=["creative_resolved.*"], include_deletion=True))
    assert evaluate(change(entity_type="ad", field="creative_resolved.headline", before="a", after="b"), p).violation
    assert evaluate(change(entity_type="ad", field="*", change_type="entity_deleted", before={"id": "1"}), p).violation
    p2 = policy(FROZEN)
    assert not evaluate(change(entity_type="ad", field="*", change_type="entity_deleted", before={"id": "1"}), p2).findings


# ------------------------------------------------------------------ prohibited_terms

TERMS = {"id": "terms", "type": "prohibited_terms", "terms": ["No.1", "絶対"], "fields": ["creative_resolved.message"]}


@pytest.mark.parametrize("text,flagged", [
    ("業界No.1のパン", True),
    ("業界no.1のパン", True),        # case-insensitive by default
    ("業界ＮＯ．１のパン", True),     # full-width normalized by NFKC
    ("業界No1のパン", False),
    ("絶対おいしい", True),
    ("おいしいパン", False),
])
def test_prohibited_terms_default_matching(text, flagged):
    ev = evaluate(change(entity_type="ad", field="creative_resolved.message", before="x", after=text), policy(TERMS))
    assert ev.violation is flagged


def test_prohibited_terms_case_sensitive():
    p = policy(dict(TERMS, case_sensitive=True))
    msg = lambda t: change(entity_type="ad", field="creative_resolved.message", before="x", after=t)
    assert not evaluate(msg("業界no.1"), p).findings
    assert evaluate(msg("業界No.1"), p).violation


def test_prohibited_terms_other_fields_ignored_and_lists_scanned():
    p = policy(TERMS)
    assert not evaluate(change(entity_type="ad", field="name", after="No.1"), p).findings
    assert evaluate(change(entity_type="ad", field="creative_resolved.message", after=["ok", "絶対"]), p).violation


def test_prohibited_terms_on_created_creative_and_preexisting_note():
    p = policy(dict(TERMS, fields=["resolved.message"]))
    created = change(entity_type="creative", field="*", change_type="entity_created",
                     after={"id": "1", "resolved": {"message": "絶対うまい"}})
    assert evaluate(created, p).violation
    p2 = policy(TERMS)
    ev = evaluate(change(entity_type="ad", field="creative_resolved.message", before="絶対A", after="絶対B"), p2)
    assert ev.violation and ev.findings[0].details["preexisting"] is True


# ------------------------------------------------------------------ change_window

WINDOW = {"id": "hours", "type": "change_window", "windows": [{"days": ["mon", "tue", "wed", "thu", "fri"], "start": "09:00", "end": "19:00"}]}


def at(ts):
    return change(attribution={"basis": "activity_match", "activities": [{"event_time": ts}]})


@pytest.mark.parametrize("ts,flagged", [
    ("2026-10-28T09:00:00+0900", False),  # start inclusive (Wed)
    ("2026-10-28T08:59:59+0900", True),
    ("2026-10-28T18:59:59+0900", False),
    ("2026-10-28T19:00:00+0900", True),   # end exclusive
    ("2026-10-31T12:00:00+0900", True),   # Saturday
    ("2026-10-28T01:00:00Z", False),      # 10:00 JST
])
def test_change_window_boundaries(ts, flagged):
    ev = evaluate(at(ts), policy(WINDOW))
    assert (ev.severity == "warning") is flagged
    assert not ev.violation  # out-of-hours is a warning, never a violation


def test_change_window_overnight():
    p = policy({"id": "night", "type": "change_window", "windows": [{"days": ["tue"], "start": "22:00", "end": "06:00"}]})
    assert not evaluate(at("2026-10-27T23:00:00+0900"), p).findings   # Tue 23:00
    assert not evaluate(at("2026-10-28T05:59:00+0900"), p).findings   # Wed early morning belongs to Tue
    assert evaluate(at("2026-10-28T06:00:00+0900"), p).findings
    assert evaluate(at("2026-10-29T01:00:00+0900"), p).findings       # Thu early morning belongs to Wed


def test_change_window_time_basis_fallbacks():
    p = policy(WINDOW)
    # updated_time inside the snapshot window is used when there is no activity
    ev = evaluate(change(updated_time="2026-10-28T03:00:00+0900"), p)
    assert ev.findings and ev.findings[0].details["basis"] == "entity.updated_time"
    # otherwise falls back to detection time (Wed 09:00 JST -> inside)
    ev = evaluate(change(updated_time="2026-10-01T03:00:00+0900"), p)
    assert not ev.findings


# ------------------------------------------------------------------ validation


@pytest.mark.parametrize("bad", [
    {"id": "x", "type": "nope"},
    {"id": "x", "type": "budget_cap", "entity_types": ["adset"], "fields": ["daily_budget"], "max": -1},
    {"id": "x", "type": "budget_cap", "entity_types": ["adset"], "fields": ["daily_budget"], "max": "20000"},
    {"id": "x", "type": "budget_cap", "entity_types": ["ad"], "fields": ["daily_budget"], "max": 1},
    {"id": "x", "type": "budget_cap", "entity_types": ["adset"], "fields": ["daily_budget"], "max": 1, "maximum": 2},
    {"id": "x", "type": "change_window", "windows": [{"days": ["funday"], "start": "09:00", "end": "10:00"}]},
    {"id": "x", "type": "change_window", "windows": [{"start": "9am", "end": "10:00"}]},
    {"id": "x", "type": "prohibited_terms", "terms": [], "fields": ["a"]},
])
def test_invalid_rules_rejected(bad):
    with pytest.raises(PolicyError):
        policy(bad)


def test_invalid_attribution_kind_rejected():
    with pytest.raises(PolicyError):
        policy(attribution={"applications": {"1": {"kind": "robot"}}})
