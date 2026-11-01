"""Send ONE tool call through the Rein proxy, as an MCP client, and print the outcome.

    this script --MCP stdio--> rein proxy (child process) --> downstream (mock, or HTTP endpoint)

The call goes through the real proxy process, so it is decided and audited exactly like an agent's call.
By default only tools listed in the policy's proxy.read_tools are sent; anything else is refused before
the proxy starts unless --allow-write is given (for the separately confirmed controlled-write step).
The client declares itself as "rein-proxy-call" in clientInfo (recorded as declared, not verified).

Examples:
  .venv/bin/python scripts/proxy_call.py --db rein.db --policy configs/policy.proxy.example.yaml \
      --mock-fixture fixtures/base --tool ads_get_ad_accounts
  .venv/bin/python scripts/proxy_call.py --db rein.db --policy configs/policy.proxy.example.yaml \
      --downstream http --downstream-url <endpoint> --tool ads_get_ad_entities --args '{"ad_account_id": "act_..."}'
The HTTP downstream reads its token from REIN_ADS_MCP_TOKEN in your shell; this script never sees or prints it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import anyio  # noqa: E402
from mcp import ClientSession, StdioServerParameters, types  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

from rein import __version__  # noqa: E402
from rein.downstream_http import DownstreamConfigError, http_downstream_from_env, sanitize_detail  # noqa: E402
from rein.policy import load_policy  # noqa: E402

PASS_ENV = ("PATH", "HOME", "LANG", "PYTHONPATH", "SYSTEMROOT", "REIN_ADS_MCP_TOKEN")


def parse(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Send one tool call through the Rein proxy and print the outcome.")
    p.add_argument("--db", required=True, help="ledger the proxy audits into")
    p.add_argument("--policy", required=True)
    p.add_argument("--downstream", choices=("mock-stdio", "http"), default="mock-stdio")
    p.add_argument("--mock-fixture")
    p.add_argument("--mock-state")
    p.add_argument("--downstream-url")
    p.add_argument("--downstream-timeout", type=float)
    p.add_argument("--tool", required=True)
    p.add_argument("--args", default="{}", help="tool arguments as a JSON object")
    p.add_argument("--allow-write", action="store_true",
                   help="allow a tool that is not in proxy.read_tools (the proxy still decides)")
    return p.parse_args(argv)


def proxy_command(a: argparse.Namespace) -> list[str]:
    cmd = ["-m", "rein.cli", "--db", a.db, "proxy", "--policy", a.policy, "--downstream", a.downstream]
    for flag, value in (("--mock-fixture", a.mock_fixture), ("--mock-state", a.mock_state),
                        ("--downstream-url", a.downstream_url), ("--downstream-timeout", a.downstream_timeout)):
        if value is not None:
            cmd += [flag, str(value)]
    return cmd


async def send(a: argparse.Namespace, arguments: dict) -> types.CallToolResult:
    env = {k: v for k, v in os.environ.items() if k in PASS_ENV}
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(ROOT / "src"), env.get("PYTHONPATH")]))
    params = StdioServerParameters(command=sys.executable, args=proxy_command(a), env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write, client_info=types.Implementation(name="rein-proxy-call",
                                                                               version=__version__)) as session:
            await session.initialize()
            return await session.call_tool(a.tool, arguments)


def main(argv: list[str] | None = None) -> int:
    a = parse(argv)
    try:
        arguments = json.loads(a.args)
    except ValueError:
        print("error: --args must be a JSON object", file=sys.stderr)
        return 2
    if not isinstance(arguments, dict):
        print("error: --args must be a JSON object", file=sys.stderr)
        return 2
    if a.tool not in load_policy(a.policy).proxy.read_tools and not a.allow_write:
        print(f"refused: {a.tool} is not in proxy.read_tools; pass --allow-write only for a confirmed write step",
              file=sys.stderr)
        return 2
    if a.downstream == "http":
        # Same checks the proxy makes at start-up, done here first so a missing token is one clean line.
        try:
            http_downstream_from_env(a.downstream_url or "")
        except DownstreamConfigError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    try:
        result = anyio.run(send, a, arguments)
    except (Exception, BaseExceptionGroup):
        print("error: the proxy process stopped before answering (its own message, if any, is above); "
              "nothing was sent", file=sys.stderr)
        return 1
    rein = (result.structuredContent or {}).get("rein")
    text = "\n".join(c.text for c in result.content if isinstance(c, types.TextContent))
    if rein:
        print(json.dumps({"decision": rein["decision"], "executed": rein.get("executed"),
                          "approval_id": rein.get("approval_id"),
                          "rules": [r["rule_id"] for r in rein.get("reasons", [])],
                          "downstream_detail": rein.get("downstream_detail")}, ensure_ascii=False))
    elif result.isError:
        downstream = (result.structuredContent or {}).get("rein_downstream") or {}
        print(json.dumps({"decision": "forwarded", "downstream_ok": False, "result": text[:500],
                          "downstream_detail": downstream.get("detail") or sanitize_detail(text)},
                         ensure_ascii=False))
    else:
        print(json.dumps({"decision": "forwarded", "downstream_ok": True}, ensure_ascii=False))
        print(json.dumps(result.structuredContent, ensure_ascii=False, indent=2) if result.structuredContent else text)
    return 0 if not result.isError else 1


if __name__ == "__main__":
    sys.exit(main())
