"""HTTP downstream demo: the Rein proxy in front of a local mock Ads MCP served over streamable HTTP.

    scripted agent --> Rein proxy --streamable HTTP, Authorization: Bearer--> mock Ads MCP (127.0.0.1)

Everything is local: the mock listens on 127.0.0.1 only, the token is the dummy `test-token-not-real`,
and nothing talks to Meta. The clock is simulated (a Tuesday, 10:00 JST) so the time-window rule is quiet.
Run: .venv/bin/python scripts/demo_http.py
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import anyio  # noqa: E402
import httpx  # noqa: E402

from rein.downstream_http import DownstreamConfigError, HttpDownstream, http_downstream_from_env  # noqa: E402
from rein.ledger import Ledger  # noqa: E402
from rein.mock_ads_mcp import MockAdsMcp  # noqa: E402
from rein.mock_ads_mcp_http import serve_in_thread  # noqa: E402
from rein.policy import load_policy  # noqa: E402
from rein.proxy import DeclaredClient, ReinProxy  # noqa: E402
from rein.timeutil import parse_time  # noqa: E402

DUMMY = "test-token-not-real"
AGENT = DeclaredClient("scripted-demo-agent", "0.1")
AS1, AS2 = "120000000000000201", "120000000000000202"


class Clock:
    def __init__(self, start: str):
        self.now = parse_time(start)

    def __call__(self):
        return self.now


class StripAuthorization(httpx.AsyncBaseTransport):
    """Drops the Authorization header on the way out, to show what a missing header does."""

    def __init__(self):
        self.inner = httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        del request.headers["authorization"]
        return await self.inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self.inner.aclose()


def rule(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


def outcome(result) -> str:
    rein = (result.structuredContent or {}).get("rein")
    if rein:
        rules = ", ".join(dict.fromkeys(r["rule_id"] for r in rein.get("reasons", []))) or "-"
        return f"{rein['decision']:<17} rules: {rules}"
    if result.isError:
        return f"forwarded, downstream failed: {result.content[0].text}"
    return "forwarded, ok"


async def run(url: str, mock: MockAdsMcp, ledger: Ledger, policy) -> None:
    clock = Clock("2026-10-27T01:00:00Z")

    rule("1. Start-up gate: HTTP downstream without REIN_ADS_MCP_TOKEN")
    try:
        http_downstream_from_env(url, env={})
        print("  started (unexpected)")
    except DownstreamConfigError as exc:
        print(f"  refused: {exc}")

    downstream = http_downstream_from_env(url, timeout=10, env={"REIN_ADS_MCP_TOKEN": DUMMY})
    proxy = ReinProxy(policy, ledger, downstream, clock=clock)
    print(f"  with the variable set: {downstream!r}")

    rule("2. Calls through the proxy (each downstream operation is its own MCP session over HTTP)")
    calls = [
        ("allow", "ads_update_entity", {"ad_account_id": "act_100000000000001", "entity_id": AS2, "entity_type": "adset", "fields": {"daily_budget": 4400}}),
        ("deny", "ads_update_entity", {"ad_account_id": "act_100000000000001", "entity_id": AS1, "entity_type": "adset", "fields": {"daily_budget": 50000}}),
        ("require_approval", "ads_update_entity", {"ad_account_id": "act_100000000000001", "entity_id": AS1, "entity_type": "adset", "fields": {"daily_budget": 9000}}),
        ("read passthrough", "ads_get_ad_entities", {"ad_account_id": "act_100000000000001", "level": "adset", "object_ids": [AS1]}),
    ]
    approval_id = None
    for label, tool, args in calls:
        before = len(mock.write_log)
        result = await proxy.handle(tool, args, AGENT)
        rein = (result.structuredContent or {}).get("rein") or {}
        approval_id = rein.get("approval_id") or approval_id
        print(f"  {label:<17} {tool} {json.dumps(args)}")
        print(f"    -> {outcome(result)}   downstream writes +{len(mock.write_log) - before}")

    rule("3. Approve the queued call: executed once, a second approval is refused")
    first = await proxy.approve(approval_id, "Staff B (self-declared)")
    second = await proxy.approve(approval_id, "Staff B (self-declared)")
    print(f"  approve #1: {first.status} (executed={first.executed})")
    print(f"  approve #2: refused, nothing re-run (status stays {second.status}, executed={second.executed})")
    print(f"  ad set 1 daily_budget on the mock: {mock.find('adset', AS1)['daily_budget']}   "
          f"downstream writes in total: {len(mock.write_log)}")

    rule("4. Missing Authorization header: the mock answers 401 and the proxy fails closed")
    no_header = HttpDownstream(url, DUMMY, timeout=10, transport=StripAuthorization())
    blind = ReinProxy(policy, ledger, no_header, clock=clock)
    before = len(mock.write_log)
    for tool, args in (("ads_get_ad_entities", {"ad_account_id": "act_100000000000001", "level": "adset", "object_ids": [AS2]}),
                       ("ads_update_entity", {"ad_account_id": "act_100000000000001", "entity_id": AS2, "entity_type": "adset", "fields": {"daily_budget": 4500}})):
        result = await blind.handle(tool, args, AGENT)
        print(f"  {tool}: {outcome(result)}")
    print(f"  downstream writes during the 401 case: {len(mock.write_log) - before}")


def main() -> None:
    logging.getLogger("mcp").setLevel(logging.ERROR)
    policy = load_policy(ROOT / "configs" / "policy.proxy.example.yaml")
    mock = MockAdsMcp(ROOT / "fixtures" / "base")
    ledger = Ledger(":memory:")
    with serve_in_thread(mock) as url:
        print(f"Local mock Ads MCP over streamable HTTP: {url} (not Meta; Bearer header required)")
        anyio.run(run, url, mock, ledger, policy)

    rule("5. Audit log and token check")
    rows = ledger.rows("proxy_audit")
    for r in rows:
        print(f"  #{r['id']:<2} {r['event']:<18} {r['tool']:<20} {r['decision']:<17} forwarded={r['forwarded']} "
              f"ok={r['downstream_ok']}  {(r['result_summary'] or '')[:40]}")
    dump = json.dumps([rows, ledger.rows("approvals")], ensure_ascii=False)
    print(f"  occurrences of the dummy token in audit and approval rows: {dump.count(DUMMY)}")
    ledger.close()
    print("\nThe token is read from REIN_ADS_MCP_TOKEN only. This demo uses a dummy value against a local mock.")


if __name__ == "__main__":
    main()
