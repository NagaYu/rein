"""Mock Ads MCP server (downstream of the Rein proxy).

THIS IS A MOCK. It is not Meta's Ads MCP server and never talks to Meta. Tool names follow
Meta's published Ads MCP tool list (ads_get_ad_entities, ads_update_entity, ads_create_campaign, ...);
the argument shapes are Rein's assumption, because the tool reference does not publish schemas.

State starts from a Phase 1 fixture directory and lives in memory. With `state_path` it is also
persisted to a local JSON file so separate processes (proxy, `rein approvals approve`) share it.

Run: python -m rein.mock_ads_mcp --fixture fixtures/base [--state mock_state.json]
"""

from __future__ import annotations

import argparse
import copy
import json
import re
from pathlib import Path
from typing import Any

import anyio
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from .client import MockAdsClient

READ_TOOLS = ("ads_get_ad_accounts", "ads_get_ad_entities", "ads_get_creatives")
WRITE_TOOLS = ("ads_update_entity", "ads_activate_entity", "ads_create_campaign", "ads_create_ad_set",
               "ads_create_ad", "ads_create_creative")

UPDATABLE = {
    "campaign": {"name", "status", "daily_budget", "lifetime_budget", "spend_cap"},
    "adset": {"name", "status", "daily_budget", "lifetime_budget", "bid_amount", "end_time"},
    "ad": {"name", "status", "creative_id"},
}
STATUSES = ("ACTIVE", "PAUSED", "ARCHIVED")
_ID = re.compile(r"^\d{1,20}$")
_ACCOUNT = re.compile(r"^act_\d{1,20}$")


class MockToolError(Exception):
    pass


def _obj(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required}


_STR, _INT = {"type": "string"}, {"type": ["integer", "string"]}
_ENTITY_TYPE = {"type": "string", "enum": ["campaign", "adset", "ad"]}

TOOL_SPECS: list[tuple[str, str, dict[str, Any], bool]] = [
    ("ads_get_ad_accounts", "[mock] List ad accounts.", _obj({}, []), True),
    ("ads_get_ad_entities", "[mock] Retrieve campaigns, ad sets or ads.",
     _obj({"account_id": _STR, "entity_type": _ENTITY_TYPE, "ids": {"type": "array", "items": _STR}}, ["entity_type"]), True),
    ("ads_get_creatives", "[mock] List ad creatives.",
     _obj({"account_id": _STR, "ids": {"type": "array", "items": _STR}}, []), True),
    ("ads_update_entity", "[mock] Update fields on an existing campaign, ad set, or ad.",
     _obj({"entity_id": _STR, "entity_type": _ENTITY_TYPE, "fields": {"type": "object"}}, ["entity_id", "fields"]), False),
    ("ads_activate_entity", "[mock] Change a paused campaign, ad set, or ad to active.",
     _obj({"entity_id": _STR, "entity_type": _ENTITY_TYPE}, ["entity_id"]), False),
    ("ads_create_campaign", "[mock] Create a new ad campaign (paused).",
     _obj({"account_id": _STR, "name": _STR, "objective": _STR, "daily_budget": _INT, "lifetime_budget": _INT,
           "special_ad_categories": {"type": "array"}}, ["account_id", "name", "objective"]), False),
    ("ads_create_ad_set", "[mock] Create an ad set under a campaign (paused).",
     _obj({"account_id": _STR, "campaign_id": _STR, "name": _STR, "daily_budget": _INT, "lifetime_budget": _INT,
           "optimization_goal": _STR, "billing_event": _STR, "targeting": {"type": "object"},
           "start_time": _STR, "end_time": _STR},
          ["account_id", "campaign_id", "name", "optimization_goal", "billing_event"]), False),
    ("ads_create_ad", "[mock] Create an ad with a creative under an ad set (paused).",
     _obj({"account_id": _STR, "adset_id": _STR, "name": _STR, "creative_id": _STR},
          ["account_id", "adset_id", "name", "creative_id"]), False),
    ("ads_create_creative", "[mock] Create a single-image link ad creative.",
     _obj({"account_id": _STR, "name": _STR, "page_id": _STR, "link": _STR, "message": _STR, "headline": _STR,
           "description": _STR, "call_to_action": _STR}, ["account_id", "name", "link", "message"]), False),
]


def tool_definitions() -> list[types.Tool]:
    return [
        types.Tool(name=name, description=desc, inputSchema=schema,
                   annotations=types.ToolAnnotations(readOnlyHint=read_only, destructiveHint=False))
        for name, desc, schema, read_only in TOOL_SPECS
    ]


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise MockToolError(msg)


def _id(value: Any, what: str) -> str:
    _require(isinstance(value, (str, int)) and not isinstance(value, bool) and _ID.match(str(value)) is not None,
             f"{what} must be a numeric id")
    return str(value)


def _budget(value: Any, what: str) -> str:
    try:
        n = int(value) if not isinstance(value, bool) else None
    except (TypeError, ValueError):
        n = None
    _require(n is not None and n > 0 and str(n) == str(value).strip(), f"{what} must be a positive integer")
    return str(n)  # Graph returns budgets as numeric strings


class MockAdsMcp:
    """Tool handlers over an in-memory copy of a fixture account."""

    def __init__(self, fixture_dir: str | Path, state_path: str | Path | None = None):
        self.state_path = Path(state_path) if state_path else None
        if self.state_path and self.state_path.exists():
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        else:
            base = MockAdsClient(fixture_dir)
            meta = json.loads((Path(fixture_dir) / "meta.json").read_text(encoding="utf-8"))
            state = {
                "account": {"id": base.account_id(), "currency": meta.get("currency"),
                            "timezone_name": meta.get("timezone_name"), "name": "Fixture account (mock)"},
                "entities": {t: base.list_entities(t) for t in ("campaign", "adset", "ad", "creative")},
                "seq": 0,
                "write_log": [],
            }
        self.state = state
        self._save()

    # ---------------------------------------------------------------- helpers

    @property
    def write_log(self) -> list[dict[str, Any]]:
        return self.state["write_log"]

    def _save(self) -> None:
        if self.state_path:
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self.state_path)

    def _rows(self, entity_type: str) -> list[dict[str, Any]]:
        return self.state["entities"][entity_type]

    def find(self, entity_type: str, entity_id: str) -> dict[str, Any] | None:
        return next((r for r in self._rows(entity_type) if str(r.get("id")) == str(entity_id)), None)

    def _locate(self, entity_id: str, entity_type: str | None) -> tuple[str, dict[str, Any]]:
        for t in ([entity_type] if entity_type else ["campaign", "adset", "ad"]):
            row = self.find(t, entity_id)
            if row is not None:
                return t, row
        raise MockToolError(f"entity {entity_id} not found")

    def _check_account(self, args: dict[str, Any]) -> None:
        acct = args.get("account_id")
        _require(isinstance(acct, str) and _ACCOUNT.match(acct) is not None, "account_id must look like act_<digits>")
        _require(acct == self.state["account"]["id"], f"unknown ad account {acct}")

    def _new_id(self) -> str:
        self.state["seq"] += 1
        return f"1299{self.state['seq']:014d}"

    def _log(self, tool: str, args: dict[str, Any], result: dict[str, Any]) -> None:
        self.write_log.append({"tool": tool, "arguments": copy.deepcopy(args), "result": result})
        self._save()

    def export_fixture(self, path: str | Path, captured_at: str) -> Path:
        """Write the current state in Phase 1 fixture layout so Watch can snapshot it."""
        from .client import EDGES

        path = Path(path)
        path.mkdir(parents=True, exist_ok=True)
        meta = {"account_id": self.state["account"]["id"], "api_version": "v26.0", "captured_at": captured_at,
                "note": "Exported from the mock Ads MCP state. Not real data."}
        (path / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        for entity_type, edge in EDGES.items():
            (path / f"{edge}.json").write_text(json.dumps({"data": self._rows(entity_type)}, ensure_ascii=False),
                                               encoding="utf-8")
        return path

    # ---------------------------------------------------------------- dispatch

    def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        _require(isinstance(args, dict), "arguments must be an object")
        handler = getattr(self, f"_tool_{name}", None)
        _require(handler is not None, f"unknown tool {name}")
        return handler(args)

    def _tool_ads_get_ad_accounts(self, args):
        return {"data": [copy.deepcopy(self.state["account"])]}

    def _tool_ads_get_ad_entities(self, args):
        t = args.get("entity_type")
        _require(t in ("campaign", "adset", "ad"), "entity_type must be campaign, adset or ad")
        ids = args.get("ids")
        rows = self._rows(t)
        if ids is not None:
            _require(isinstance(ids, list), "ids must be a list")
            wanted = {str(i) for i in ids}
            rows = [r for r in rows if str(r.get("id")) in wanted]
        return {"data": copy.deepcopy(rows)}

    def _tool_ads_get_creatives(self, args):
        ids = args.get("ids")
        rows = self._rows("creative")
        if ids is not None:
            _require(isinstance(ids, list), "ids must be a list")
            wanted = {str(i) for i in ids}
            rows = [r for r in rows if str(r.get("id")) in wanted]
        return {"data": copy.deepcopy(rows)}

    def _tool_ads_update_entity(self, args):
        entity_id = _id(args.get("entity_id"), "entity_id")
        etype, row = self._locate(entity_id, args.get("entity_type"))
        fields = args.get("fields")
        _require(isinstance(fields, dict) and fields, "fields must be a non-empty object")
        unknown = set(fields) - UPDATABLE[etype]
        _require(not unknown, f"fields not updatable on {etype}: {sorted(unknown)}")
        staged = {}
        for key, value in fields.items():
            if key in ("daily_budget", "lifetime_budget", "spend_cap", "bid_amount"):
                staged[key] = _budget(value, key)
            elif key == "status":
                _require(value in STATUSES, f"status must be one of {STATUSES}")
                staged[key] = value
            elif key == "creative_id":
                cid = _id(value, "creative_id")
                _require(self.find("creative", cid) is not None, f"creative {cid} not found")
                staged["creative"] = {"id": cid}
            else:
                _require(isinstance(value, str) and value, f"{key} must be a non-empty string")
                staged[key] = value
        row.update(staged)
        result = {"success": True, "id": entity_id, "entity_type": etype}
        self._log("ads_update_entity", args, result)
        return result

    def _tool_ads_activate_entity(self, args):
        entity_id = _id(args.get("entity_id"), "entity_id")
        etype, row = self._locate(entity_id, args.get("entity_type"))
        row["status"] = "ACTIVE"
        result = {"success": True, "id": entity_id, "entity_type": etype, "status": "ACTIVE"}
        self._log("ads_activate_entity", args, result)
        return result

    def _tool_ads_create_campaign(self, args):
        self._check_account(args)
        row = {"id": self._new_id(), "name": args.get("name"), "objective": args.get("objective"),
               "status": "PAUSED", "special_ad_categories": args.get("special_ad_categories", [])}
        _require(isinstance(row["name"], str) and row["name"], "name is required")
        _require(isinstance(row["objective"], str) and row["objective"], "objective is required")
        for key in ("daily_budget", "lifetime_budget"):
            if key in args:
                row[key] = _budget(args[key], key)
        self._rows("campaign").append(row)
        result = {"id": row["id"], "status": "PAUSED"}
        self._log("ads_create_campaign", args, result)
        return result

    def _tool_ads_create_ad_set(self, args):
        self._check_account(args)
        campaign_id = _id(args.get("campaign_id"), "campaign_id")
        _require(self.find("campaign", campaign_id) is not None, f"campaign {campaign_id} not found")
        row = {"id": self._new_id(), "campaign_id": campaign_id, "status": "PAUSED"}
        for key in ("name", "optimization_goal", "billing_event"):
            _require(isinstance(args.get(key), str) and args[key], f"{key} is required")
            row[key] = args[key]
        for key in ("daily_budget", "lifetime_budget"):
            if key in args:
                row[key] = _budget(args[key], key)
        for key in ("targeting", "start_time", "end_time"):
            if key in args:
                row[key] = copy.deepcopy(args[key])
        self._rows("adset").append(row)
        result = {"id": row["id"], "status": "PAUSED"}
        self._log("ads_create_ad_set", args, result)
        return result

    def _tool_ads_create_ad(self, args):
        self._check_account(args)
        adset_id = _id(args.get("adset_id"), "adset_id")
        adset = self.find("adset", adset_id)
        _require(adset is not None, f"adset {adset_id} not found")
        creative_id = _id(args.get("creative_id"), "creative_id")
        _require(self.find("creative", creative_id) is not None, f"creative {creative_id} not found")
        _require(isinstance(args.get("name"), str) and args["name"], "name is required")
        row = {"id": self._new_id(), "name": args["name"], "adset_id": adset_id,
               "campaign_id": adset.get("campaign_id"), "status": "PAUSED", "creative": {"id": creative_id}}
        self._rows("ad").append(row)
        result = {"id": row["id"], "status": "PAUSED"}
        self._log("ads_create_ad", args, result)
        return result

    def _tool_ads_create_creative(self, args):
        self._check_account(args)
        for key in ("name", "link", "message"):
            _require(isinstance(args.get(key), str) and args[key], f"{key} is required")
        link_data = {"link": args["link"], "message": args["message"]}
        if args.get("headline"):
            link_data["name"] = args["headline"]
        if args.get("description"):
            link_data["description"] = args["description"]
        if args.get("call_to_action"):
            link_data["call_to_action"] = {"type": args["call_to_action"]}
        row = {"id": self._new_id(), "name": args["name"], "status": "ACTIVE",
               "object_story_spec": {"page_id": args.get("page_id", "990000000000009"), "link_data": link_data}}
        self._rows("creative").append(row)
        result = {"id": row["id"]}
        self._log("ads_create_creative", args, result)
        return result


def build_server(mock: MockAdsMcp) -> Server:
    server: Server = Server("rein-mock-ads-mcp", version="0.2.0",
                            instructions="Mock of Meta's Ads MCP for Rein tests. Not connected to Meta.")

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:
        return tool_definitions()

    @server.call_tool(validate_input=False)
    async def _call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        try:
            result = mock.call(name, arguments)
        except MockToolError as exc:
            return types.CallToolResult(content=[types.TextContent(type="text", text=f"error: {exc}")], isError=True)
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False))],
            structuredContent=result, isError=False)

    return server


async def serve_stdio(mock: MockAdsMcp) -> None:
    server = build_server(mock)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="rein-mock-ads-mcp", description="Mock Ads MCP server (stdio). Not Meta.")
    parser.add_argument("--fixture", required=True)
    parser.add_argument("--state", help="persist mock state to this JSON file")
    args = parser.parse_args(argv)
    anyio.run(serve_stdio, MockAdsMcp(args.fixture, args.state))


if __name__ == "__main__":
    main()
