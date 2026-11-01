"""Mock Ads MCP server (downstream of the Rein proxy).

THIS IS A MOCK. It is not Meta's Ads MCP server and never talks to Meta. Tool names follow
Meta's published Ads MCP tool list (ads_get_ad_entities, ads_update_entity, ads_create_campaign, ...);
since Phase 5C the argument names and required lists follow the real tools/list (discover-live.json,
copied into fixtures/discovery/assumed_tools_list.json). Types, enums and nested shapes are still Rein's
assumption: the discovery report records names only.

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

READ_TOOLS = ("ads_get_ad_accounts", "ads_get_ad_entities", "ads_get_creatives", "ads_account_get_activity_logs",
              "ads_get_ad_images", "ads_get_ad_videos", "ads_get_custom_audience", "ads_get_ad_account_pages",
              "ads_get_ig_accounts")
WRITE_TOOLS = ("ads_update_entity", "ads_activate_entity", "ads_create_campaign", "ads_create_ad_set",
               "ads_create_ad", "ads_create_creative")

UPDATABLE = {
    "campaign": {"name", "status", "daily_budget", "lifetime_budget", "spend_cap"},
    "adset": {"name", "status", "daily_budget", "lifetime_budget", "bid_amount", "end_time"},
    "ad": {"name", "status", "creative_id"},
}
STATUSES = ("ACTIVE", "PAUSED", "ARCHIVED")
_ID = re.compile(r"^\d{1,20}$")
_ACCOUNT = re.compile(r"^(act_)?\d{1,20}$")
SHAPES_FILE = Path(__file__).resolve().parents[2] / "fixtures" / "discovery" / "assumed_tools_list.json"
ENTITY_TYPES = {"campaign": "campaign", "adset": "adset", "ad_set": "adset", "ad": "ad"}


class MockToolError(Exception):
    pass


def tool_definitions() -> list[types.Tool]:
    """The real tool names, property names, required lists and annotations (types unknown: empty schemas)."""
    doc = json.loads(SHAPES_FILE.read_text(encoding="utf-8"))
    return [types.Tool(name=t["name"], description=f"[mock] {t['name']}", inputSchema=t["inputSchema"],
                       annotations=types.ToolAnnotations(**t["annotations"]))
            for t in doc["tools"]]


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

    def __init__(self, fixture_dir: str | Path, state_path: str | Path | None = None, *, draft_lifecycle: bool = False):
        # draft_lifecycle (Phase 7) models what the real Ads MCP showed in Phase 5: ads_create_campaign answers a
        # DRAFT descriptor that is not an account entity, until ads_activate_entity publishes it.
        self.draft_lifecycle = draft_lifecycle
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
                "activities": base.list_activities(),
                "seq": 0,
                "write_log": [],
            }
        state.setdefault("drafts", {})
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

    def _check_account(self, args: dict[str, Any], required: bool = True) -> None:
        acct = args.get("ad_account_id")
        if acct is None and not required:
            return
        _require(isinstance(acct, str) and _ACCOUNT.match(acct) is not None,
                 "ad_account_id must look like act_<digits> or <digits>")
        _require(("act_" + acct if not acct.startswith("act_") else acct) == self.state["account"]["id"],
                 f"(mock) ad account {acct} not found or not accessible with this token")

    def _etype(self, value: Any) -> str:
        etype = ENTITY_TYPES.get(value.lower()) if isinstance(value, str) else None
        _require(etype is not None, "entity_type must be campaign, adset (ad_set) or ad")
        return etype

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

    @staticmethod
    def _wanted(args: dict[str, Any], key: str) -> set[str] | None:
        ids = args.get(key)
        if ids is None:
            return None
        _require(isinstance(ids, list), f"{key} must be a list")
        return {str(i) for i in ids}

    def _tool_ads_get_ad_entities(self, args):
        self._check_account(args)
        levels = [self._etype(args["level"])] if "level" in args else ["campaign", "adset", "ad"]
        wanted = self._wanted(args, "object_ids")
        rows = [r for t in levels for r in self._rows(t) if wanted is None or str(r.get("id")) in wanted]
        result = {"data": copy.deepcopy(rows)}
        if self.draft_lifecycle and wanted:
            missing = sorted(wanted - {str(r.get("id")) for r in rows})
            if missing:
                # The same note the real server gave for a draft ID (Phase 5).
                result["additional_info"] = ("The following object_ids are not campaign entities in the requested "
                                             f"ad account: {', '.join(missing)}.")
        return result

    def _tool_ads_get_creatives(self, args):
        self._check_account(args, required=False)
        wanted = self._wanted(args, "creative_ids")
        rows = [r for r in self._rows("creative") if wanted is None or str(r.get("id")) in wanted]
        return {"data": copy.deepcopy(rows)}

    def _tool_ads_account_get_activity_logs(self, args):
        self._check_account(args)
        return {"data": copy.deepcopy(self.state.get("activities", []))}

    def _tool_ads_get_ad_images(self, args):
        self._check_account(args)
        hashes = sorted({h for c in self._rows("creative")
                         for h in [((c.get("object_story_spec") or {}).get("link_data") or {}).get("image_hash")] if h})
        return {"data": [{"hash": h, "name": f"{h}.jpg"} for h in hashes]}

    def _tool_ads_get_ad_videos(self, args):
        self._check_account(args)
        return {"data": []}

    def _tool_ads_get_custom_audience(self, args):
        audience = args.get("custom_audience_id")
        _require(isinstance(audience, str) and audience, "custom_audience_id is required")
        raise MockToolError(f"custom audience {audience} not found (the mock has none)")

    def _tool_ads_get_ad_account_pages(self, args):
        self._check_account(args)
        pages = sorted({str((c.get("object_story_spec") or {}).get("page_id")) for c in self._rows("creative")
                        if (c.get("object_story_spec") or {}).get("page_id")})
        return {"data": [{"id": p, "name": "Fixture page (mock)"} for p in pages]}

    def _tool_ads_get_ig_accounts(self, args):
        self._check_account(args)
        return {"data": []}

    def _tool_ads_update_entity(self, args):
        self._check_account(args)
        entity_id = _id(args.get("entity_id"), "entity_id")
        etype, row = self._locate(entity_id, self._etype(args.get("entity_type")))
        fields = args.get("fields") or {}
        _require(isinstance(fields, dict), "fields must be an object")
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
        self._check_account(args)
        entity_id = _id(args.get("entity_id"), "entity_id")
        if self.draft_lifecycle and entity_id in self.state["drafts"]:
            return self._publish_draft(entity_id, args)
        etype, row = self._locate(entity_id, self._etype(args.get("entity_type")))
        row["status"] = "ACTIVE"
        result = {"success": True, "id": entity_id, "entity_type": etype, "status": "ACTIVE"}
        self._log("ads_activate_entity", args, result)
        return result

    def _publish_draft(self, draft_id: str, args: dict[str, Any]) -> dict[str, Any]:
        if args.get("publish_as_active") is not True:
            raise MockToolError(f"(mock) {draft_id} is a draft that has not been published; activating it needs "
                                "publish_as_active: true")
        draft = self.state["drafts"].pop(draft_id)
        row = {"id": draft_id, "name": draft["name"], "objective": draft["objective"],
               "buying_type": draft["buying_type"], "status": "PAUSED",
               "special_ad_categories": draft.get("special_ad_categories", [])}
        self._rows("campaign").append(row)
        self.state["activities"].append({
            "event_type": "create_campaign_group", "translated_event_type": "create campaign (published draft)",
            "event_time": "2026-11-02T10:00:00+0900", "object_id": draft_id, "object_type": "CAMPAIGN",
            "actor_id": "990000000000555", "actor_name": "Staff A (fictional)",
            "application_id": "900000000000004", "application_name": "Ads MCP (mock label)",
            "extra_data": json.dumps({"published_from_draft": True})})
        result = {"success": True, "id": draft_id, "entity_type": "campaign", "status": "PAUSED", "published": True}
        self._log("ads_activate_entity", args, result)
        return result

    def _tool_ads_create_campaign(self, args):
        self._check_account(args)
        if self.draft_lifecycle:
            return self._create_draft_campaign(args)
        for key in ("campaign_name", "objective", "buying_type"):
            _require(isinstance(args.get(key), str) and args[key], f"{key} is required")
        row = {"id": self._new_id(), "name": args["campaign_name"], "objective": args["objective"],
               "buying_type": args["buying_type"], "status": "PAUSED",
               "special_ad_categories": args.get("special_ad_categories", [])}
        for arg, key in (("campaign_daily_budget", "daily_budget"), ("campaign_lifetime_budget", "lifetime_budget"),
                         ("campaign_spend_cap", "spend_cap")):
            if arg in args:
                row[key] = _budget(args[arg], arg)
        self._rows("campaign").append(row)
        result = {"id": row["id"], "status": "PAUSED"}
        self._log("ads_create_campaign", args, result)
        return result

    def _create_draft_campaign(self, args):
        for key in ("campaign_name", "objective", "buying_type"):
            _require(isinstance(args.get(key), str) and args[key], f"{key} is required")
        draft_id = self._new_id()
        spec = {"name": args["campaign_name"], "objective": args["objective"],
                "special_ad_categories": args.get("special_ad_categories", []), "status": "ACTIVE",
                "buying_type": args["buying_type"]}
        self.state["drafts"][draft_id] = spec
        # Shaped like the real answer from Phase 5 (strings inside, as the server sent them).
        result = {"campaign_id": draft_id, "status": "DRAFT", "spec": json.dumps(spec, ensure_ascii=False),
                  "valid_optimization_goals": json.dumps(["LINK_CLICKS", "REACH"]),
                  "recommended_optimization_goal": "LINK_CLICKS", "active_errors": "[]"}
        self._log("ads_create_campaign", args, result)
        return result

    def _tool_ads_create_ad_set(self, args):
        self._check_account(args)
        campaign_id = _id(args.get("campaign_id"), "campaign_id")
        _require(self.find("campaign", campaign_id) is not None, f"campaign {campaign_id} not found")
        _require(isinstance(args.get("targeting"), dict), "targeting is required")
        row = {"id": self._new_id(), "campaign_id": campaign_id, "status": "PAUSED"}
        for arg, key in (("ad_set_name", "name"), ("optimization_goal", "optimization_goal"),
                         ("billing_event", "billing_event")):
            _require(isinstance(args.get(arg), str) and args[arg], f"{arg} is required")
            row[key] = args[arg]
        for key in ("daily_budget", "lifetime_budget", "bid_amount"):
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
        adset_id = _id(args.get("ad_set_id"), "ad_set_id")
        adset = self.find("adset", adset_id)
        _require(adset is not None, f"ad set {adset_id} not found")
        _require(isinstance(args.get("ad_name"), str) and args["ad_name"], "ad_name is required")
        row = {"id": self._new_id(), "name": args["ad_name"], "adset_id": adset_id,
               "campaign_id": adset.get("campaign_id"), "status": "PAUSED"}
        creative = args.get("creative")
        if creative is not None:
            _require(isinstance(creative, dict) and "creative_id" in creative,
                     "the mock only supports creative={'creative_id': ...}")
            creative_id = _id(creative["creative_id"], "creative.creative_id")
            _require(self.find("creative", creative_id) is not None, f"creative {creative_id} not found")
            row["creative"] = {"id": creative_id}
        self._rows("ad").append(row)
        result = {"id": row["id"], "status": "PAUSED"}
        self._log("ads_create_ad", args, result)
        return result

    def _tool_ads_create_creative(self, args):
        from .proxy import Uninterpretable, creative_raw_from_args

        self._check_account(args)
        clean = {k: v for k, v in args.items()
                 if k not in ("ad_account_id", "advertiser_job", "advertiser_request", "client_conversation_id",
                              "scheduled_task_run")}
        try:
            raw = creative_raw_from_args(clean)
        except (Uninterpretable, KeyError) as exc:
            raise MockToolError(f"creative not supported by the mock: {exc}") from None
        spec = raw.setdefault("object_story_spec", {})
        spec.setdefault("page_id", "990000000000009")
        row = {"id": self._new_id(), "name": raw.get("name", "creative"), "status": "ACTIVE", **raw}
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
    parser.add_argument("--draft-lifecycle", action="store_true",
                        help="creates answer DRAFT descriptors that only publish_as_active makes real")
    args = parser.parse_args(argv)
    anyio.run(serve_stdio, MockAdsMcp(args.fixture, args.state, draft_lifecycle=args.draft_lifecycle))


if __name__ == "__main__":
    main()
