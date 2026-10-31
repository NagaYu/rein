from __future__ import annotations

import copy
import json
from pathlib import Path

from rein.client import MockAdsClient
from rein.creative import (creative_view, load_attributes, load_creative_rules, place_on_ad, register_original,
                           run_check)
from rein.ledger import Ledger
from rein.pipeline import ingest
from rein.snapshot import normalize_entity
from rein.timeutil import parse_time

ROOT = Path(__file__).resolve().parent.parent
CREATIVE = ROOT / "fixtures" / "creative"
RULES_PATH = ROOT / "configs" / "creative_rules.example.yaml"
ACCOUNT2 = "act_100000000000002"
K_AD1, K_AD2, K_AD3 = "130000000000000301", "130000000000000302", "130000000000000303"
K_CR1, K_CR2, K_CR3 = "130000000000000401", "130000000000000402", "130000000000000403"


class Clock:
    def __init__(self, start: str):
        self.now = parse_time(start)

    def __call__(self):
        return self.now


def cases() -> list[dict]:
    return json.loads((CREATIVE / "cases.json").read_text(encoding="utf-8"))["cases"]


def rules():
    return load_creative_rules(RULES_PATH)


def attributes():
    return load_attributes(CREATIVE / "attributes.json")


def raw_base_creative(creative_id: str) -> dict:
    data = json.loads((CREATIVE / "base" / "adcreatives.json").read_text(encoding="utf-8"))["data"]
    return copy.deepcopy(next(c for c in data if c["id"] == creative_id))


def build_candidate(spec: dict) -> dict:
    """A raw creative: a copy of a base creative with link_data overrides (Meta field names)."""
    raw = raw_base_creative(spec["from"])
    raw["id"] = spec["id"]
    raw["object_story_spec"]["link_data"].update(copy.deepcopy(spec.get("link_data") or {}))
    return raw


def registry(originals: bool = True):
    """Ledger with fixtures/creative/base ingested and (optionally) the fixture originals registered."""
    clock = Clock("2026-10-28T00:00:00Z")
    ledger = Ledger(":memory:", clock=clock)
    res = ingest(ledger, MockAdsClient(CREATIVE / "base"))
    snap = ledger.load_snapshot(res.snapshot_id)
    if originals:
        reg = json.loads((CREATIVE / "originals.json").read_text(encoding="utf-8"))
        clock.now = parse_time(reg["registered_at"])
        for o in reg["originals"]:
            register_original(ledger, snap, creative_id=o["creative_id"], by=reg["registered_by"], by_source="fixture",
                              substantiation=o.get("substantiation"))
    return ledger, snap


def run_case(case: dict, ledger=None, snap=None, **kw) -> dict:
    if ledger is None:
        ledger, snap = registry()
    raw = build_candidate(case["candidate"])
    entity = normalize_entity("creative", raw)
    if case.get("ad_id"):
        snap = place_on_ad(snap, case["ad_id"], raw["id"], entity)
    return run_check(ledger, rules(), account_id=ACCOUNT2, creative_id=raw["id"], candidate=creative_view(entity),
                     snapshot=snap, attributes=attributes(), **kw)


def rule_ids(result: dict) -> list[str]:
    return sorted(d["rule_id"] for d in result["detections"])
