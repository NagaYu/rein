from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import anyio

from rein.ledger import Ledger
from rein.mock_ads_mcp import MockAdsMcp
from rein.policy import load_policy, parse_policy
from rein.proxy import DeclaredClient, InProcessDownstream, ReinProxy
from rein.timeutil import parse_time

ROOT = Path(__file__).resolve().parent.parent
PROXY_POLICY = ROOT / "configs" / "policy.proxy.example.yaml"
SCENARIO_FILE = ROOT / "fixtures" / "proxy" / "scenarios.json"
BASE = ROOT / "fixtures" / "base"
AGENT = DeclaredClient("scripted-agent", "1.0")

AS1, AS2, AD2 = "120000000000000201", "120000000000000202", "120000000000000302"
CR1, CR2 = "120000000000000401", "120000000000000402"
ACCOUNT = "act_100000000000001"


def scenarios() -> dict:
    return json.loads(SCENARIO_FILE.read_text(encoding="utf-8"))


class Clock:
    def __init__(self, start: str | datetime):
        self.now = parse_time(start)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw) -> None:
        self.now += timedelta(**kw)


def make_proxy(clock: str | Clock = "2026-10-28T05:00:00Z", policy=None, downstream=None, ledger=None):
    clock = clock if isinstance(clock, Clock) else Clock(clock)
    mock = MockAdsMcp(BASE)
    downstream = downstream or InProcessDownstream(mock)
    proxy = ReinProxy(policy or load_policy(PROXY_POLICY), ledger or Ledger(":memory:"), downstream, clock=clock)
    return proxy, mock, clock


def call(proxy, tool, arguments, client=AGENT):
    return anyio.run(proxy.handle, tool, arguments, client)


def decision_of(result) -> str:
    if result.isError and result.structuredContent and "rein" in result.structuredContent:
        return result.structuredContent["rein"]["decision"]
    return "forwarded-error" if result.isError else "forwarded"


def approve(proxy, approval_id, operator="tester"):
    return anyio.run(proxy.approve, approval_id, operator)


def policy_with(**overrides):
    import yaml
    raw = yaml.safe_load(PROXY_POLICY.read_text(encoding="utf-8"))
    for rule_id, changes in overrides.items():
        for rule in raw["rules"]:
            if rule["id"] == rule_id:
                rule.update(changes)
    return parse_policy(raw)
