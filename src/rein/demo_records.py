"""Build a ledger holding Phase 1 + 2 + 3 demo records, for the evidence-pack demo and its tests.

Everything is fixtures and mocks (fictional accounts); nothing touches Meta or the network.
The clock is simulated so the records have reproducible times:

  10-27 00:00Z  account 1 baseline snapshot; originals registered
  10-27 01:00Z  agent calls through the proxy (fixtures/proxy/scenarios.json), approvals decided
  10-28 00:00Z  account 1 snapshot (fixtures/evidence/after): proxy-forwarded budgets + other actors
  10-28 00:05Z  Watch check + Creative Check; 01:00Z revert plan executed on the mock
  10-28 00:00Z  account 2 baseline (fixtures/creative/base); originals registered at 02:00Z
  10-29 00:00Z  account 2 snapshot (fixtures/creative/after): AI rewrites; checked at 00:05Z
  10-29 18:00Z  the night-time approval expires
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import anyio

from .alerts import FileAlertSink
from .client import MockAdsClient
from .creative import CreativeWatch, load_attributes, load_creative_rules, prohibited_from_policy, register_original
from .ledger import Ledger
from .mock_ads_mcp import MockAdsMcp
from .pipeline import ingest, watch_check
from .policy import load_policy
from .proxy import DeclaredClient, InProcessDownstream, ReinProxy
from .revert import execute_plan, plan_for_changes
from .timeutil import parse_time

PERIOD = ("2026-10-27T00:00:00Z", "2026-10-31T00:00:00Z")
AGENT = DeclaredClient("scripted-demo-agent", "0.1")


class Clock:
    def __init__(self, start: str):
        self.now = parse_time(start)

    def __call__(self) -> datetime:
        return self.now

    def at(self, stamp: str) -> "Clock":
        self.now = parse_time(stamp)
        return self


def paths(root: str | Path) -> dict[str, Path]:
    root = Path(root)
    return {
        "policy": root / "configs" / "policy.example.yaml",
        "proxy_policy": root / "configs" / "policy.proxy.example.yaml",
        "creative_rules": root / "configs" / "creative_rules.example.yaml",
        "attributes": root / "fixtures" / "creative" / "attributes.json",
        "originals": root / "fixtures" / "creative" / "originals.json",
        "base": root / "fixtures" / "base",
        "evidence_after": root / "fixtures" / "evidence" / "after",
        "creative_base": root / "fixtures" / "creative" / "base",
        "creative_after": root / "fixtures" / "creative" / "after",
        "proxy_scenarios": root / "fixtures" / "proxy" / "scenarios.json",
    }


async def _proxy_session(proxy: ReinProxy, clock: Clock, scenarios: dict[str, Any]) -> dict[int, str]:
    queued = {}
    for spec in scenarios["calls"]:
        clock.at(spec.get("clock", "2026-10-27T01:00:00Z"))
        result = await proxy.handle(spec["tool"], spec["arguments"], AGENT)
        rein = (result.structuredContent or {}).get("rein") or {}
        if rein.get("decision") == "require_approval":
            queued[spec["id"]] = rein["approval_id"]
    clock.at("2026-10-27T02:00:00Z")
    await proxy.approve(queued[3], "運用責任者B（自己申告）")
    clock.at("2026-10-27T02:05:00Z")
    proxy.reject(queued[5], "運用責任者B（自己申告）", "今期は新規キャンペーンを作らない")
    clock.at("2026-10-27T02:10:00Z")
    await proxy.approve(queued[3], "運用責任者B（自己申告）")  # double click: refused, not re-run
    return queued


def build_demo_ledger(db_path: str | Path, root: str | Path, workdir: str | Path) -> dict[str, Any]:
    p = paths(root)
    workdir = Path(workdir)
    clock = Clock(PERIOD[0])
    ledger = Ledger(db_path, clock=clock)
    policy = load_policy(p["policy"])
    rules = load_creative_rules(p["creative_rules"])
    watch = CreativeWatch(rules, load_attributes(p["attributes"]), prohibited_from_policy(policy))
    sinks = [FileAlertSink(workdir / "alerts.jsonl")]

    # Account 1: baseline, approved originals (no category/brand attributes are declared for it).
    base = ingest(ledger, MockAdsClient(p["base"]), policy.attribution)
    snap = ledger.load_snapshot(base.snapshot_id)
    clock.at("2026-10-27T00:30:00Z")
    for creative_id in sorted(snap.entities["creative"]):
        register_original(ledger, snap, creative_id=creative_id, by="広告主担当A（架空）", by_source="demo")

    # Phase 2: an agent works through the proxy against the mock Ads MCP (which starts from fixtures/base).
    proxy_policy = load_policy(p["proxy_policy"])
    mock = MockAdsMcp(p["base"])
    proxy = ReinProxy(proxy_policy, ledger, InProcessDownstream(mock), clock=clock)
    queued = anyio.run(_proxy_session, proxy, clock, json.loads(p["proxy_scenarios"].read_text(encoding="utf-8")))

    # Phase 1 + 3: the next snapshot shows the proxied budgets and changes by other actors.
    clock.at("2026-10-28T00:00:00Z")
    ingest(ledger, MockAdsClient(p["evidence_after"]), policy.attribution)
    clock.at("2026-10-28T00:05:00Z")
    first = watch_check(ledger, policy, sinks, creative=watch)
    clock.at("2026-10-28T01:00:00Z")
    violations = [r.change for r in first.changes if r.evaluation.severity == "violation"]
    plan = plan_for_changes(violations, ledger)
    target = MockAdsClient(p["evidence_after"])
    results = execute_plan(plan, target)
    ledger.record_revert_run(target.source, plan.change_ids, plan.to_dict(), [r.__dict__ for r in results])

    # Account 2: Creative Check with categories, brands and a substantiation note.
    clock.at("2026-10-28T00:00:00Z")
    kbase = ingest(ledger, MockAdsClient(p["creative_base"]), policy.attribution)
    ksnap = ledger.load_snapshot(kbase.snapshot_id)
    reg = json.loads(p["originals"].read_text(encoding="utf-8"))
    clock.at(reg["registered_at"])
    for o in reg["originals"]:
        register_original(ledger, ksnap, creative_id=o["creative_id"], by=reg["registered_by"], by_source="fixture",
                          substantiation=o.get("substantiation"))
    clock.at("2026-10-29T00:00:00Z")
    ingest(ledger, MockAdsClient(p["creative_after"]), policy.attribution)
    clock.at("2026-10-29T00:05:00Z")
    second = watch_check(ledger, policy, sinks, creative=watch)

    clock.now = parse_time("2026-10-28T18:00:00Z") + timedelta(hours=float(proxy_policy.proxy.approval_ttl_hours))
    proxy.expire_due()
    return {"ledger": ledger, "clock": clock, "queued": queued, "watch": [first, second], "revert": results,
            "mock_writes": len(mock.write_log), "policy_files": [p["policy"], p["proxy_policy"], p["creative_rules"]]}
