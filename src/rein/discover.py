"""`rein proxy discover`: how well Rein's interpreter fits a downstream's tools/list. Read-only.

Discovery sends `initialize` and `tools/list`, and nothing else. This is enforced by the session
class every discovery connection uses (ListOnlyClientSession): any other request, `tools/call`
included, raises before it leaves the process. The report is built from the tool list alone.

The report says, per tool, how the proxy would treat it (passthrough / evaluated / denied) and by
which rule, whether the interpreter has an argument mapping, and where Rein's assumed argument
shapes and the downstream's declared schemas disagree. Downstream annotations (readOnlyHint, ...)
are shown as hints for the owner; they never change how the proxy classifies a tool.

The report never contains credentials: discovery never sees the token value (only the HTTP
transport holds it), endpoints are reduced to scheme/host/port/path, and the final text is
scrubbed for the token as a last line of defence.
"""

from __future__ import annotations

import json
from typing import Any, Iterable

from mcp import ClientSession, types

from .downstream_http import REDACTED, DiscoveryViolation
from .policy import Policy
from .proxy import AGENT_METADATA, ASSUMED_ARGUMENTS, KNOWN_WRITE_TOOLS, LOOKUP_ARGUMENTS, UPDATE_FIELDS

REPORT_KIND = "rein.discovery_report"
REPORT_VERSION = 1
ALLOWED_REQUESTS = frozenset({"initialize", "tools/list"})
ALLOWED_NOTIFICATIONS = frozenset({"notifications/initialized"})
NOTE = ("Read-only discovery: only initialize and tools/list were sent. Rein's argument shapes are assumptions "
        "(Meta publishes no schemas). Annotations are the downstream's own hints and are not trusted for "
        "classification: only policy proxy.read_tools makes a tool a passthrough read.")

GAP_TEXT = {
    "write_without_mapping": "not in proxy.read_tools and Rein has no interpreter: every call is denied",
    "read_hint_not_allowlisted": "looks like a read (annotation or name) but is not in proxy.read_tools: denied "
                                 "until the owner reviews it and adds it to the allowlist",
    "allowlisted_but_hints_write": "in proxy.read_tools, but the downstream does not mark it read-only: review "
                                   "before trusting the allowlist entry",
    "schema_fields_not_in_rein": "the schema has arguments Rein neither interprets nor accepts as benign: calls "
                                 "using them are uninterpretable and denied (by design until reviewed)",
    "rein_fields_not_in_schema": "Rein accepts arguments the schema does not declare",
    "schema_requires_unknown_to_rein": "the schema requires arguments Rein does not accept: every valid call "
                                       "is denied",
    "rein_requires_not_required_by_schema": "Rein requires arguments the schema treats as optional: calls "
                                            "without them are denied",
    "update_fields_not_in_rein": "the schema allows update fields Rein does not interpret: those updates are denied",
    "rein_update_fields_not_in_schema": "Rein interprets update fields the schema does not declare",
    "lookup_args_not_in_schema": "Rein sends these arguments when it reads current values: lookups may fail and "
                                 "turn writes into approvals",
    "lookup_omits_required_args": "the schema requires arguments Rein's current-value lookups do not send: lookups "
                                  "may fail and turn writes into approvals",
    "schema_missing": "the tool declares no object schema with properties: argument fit cannot be checked",
}


class ListOnlyClientSession(ClientSession):
    """An MCP client session that can initialize and list tools, and refuses everything else."""

    async def send_request(self, request: Any, *args: Any, **kwargs: Any) -> Any:
        method = getattr(getattr(request, "root", request), "method", None)
        if method not in ALLOWED_REQUESTS:
            raise DiscoveryViolation(f"discovery refuses to send {method!r}")
        return await super().send_request(request, *args, **kwargs)

    async def send_notification(self, notification: Any, *args: Any, **kwargs: Any) -> None:
        method = getattr(getattr(notification, "root", notification), "method", None)
        if method not in ALLOWED_NOTIFICATIONS:
            raise DiscoveryViolation(f"discovery refuses to send {method!r}")
        await super().send_notification(notification, *args, **kwargs)

    async def call_tool(self, *args: Any, **kwargs: Any) -> Any:
        raise DiscoveryViolation("discovery never calls tools")


async def list_all_tools(session: ClientSession) -> list[types.Tool]:
    tools: list[types.Tool] = []
    cursor: str | None = None
    while True:
        page = await session.list_tools(cursor)
        tools.extend(page.tools)
        cursor = page.nextCursor
        if not cursor:
            return tools


# --------------------------------------------------------------------------- report


def _schema(tool: dict[str, Any]) -> tuple[set[str], set[str], dict[str, Any]] | None:
    schema = tool.get("inputSchema")
    if not isinstance(schema, dict) or not isinstance(schema.get("properties"), dict):
        return None
    props = schema["properties"]
    required = schema.get("required") if isinstance(schema.get("required"), list) else []
    return set(props), {str(r) for r in required}, props


def _read_hint(tool: dict[str, Any]) -> tuple[bool, str | None]:
    ann = tool.get("annotations") or {}
    if ann.get("readOnlyHint") is True:
        return True, "annotation readOnlyHint=true"
    name = tool["name"]
    if name.startswith("ads_get_") or "_get_" in name or name.endswith("_search"):
        return True, "name looks like a read"
    return False, None


def _gap(code: str, fields: Iterable[str] | None = None) -> dict[str, Any]:
    gap: dict[str, Any] = {"code": code, "detail": GAP_TEXT[code]}
    if fields is not None:
        gap["fields"] = sorted(fields)
    return gap


def _tool_entry(tool: dict[str, Any], policy: Policy) -> dict[str, Any]:
    name = tool["name"]
    ann = {k: v for k, v in sorted((tool.get("annotations") or {}).items()) if v is not None and k != "title"}
    schema = _schema(tool)
    gaps: list[dict[str, Any]] = []
    looks_read, hint = _read_hint(tool)

    if name in policy.proxy.read_tools:
        classification, rule, outcome = "read", "listed in policy proxy.read_tools", "passthrough"
        if ann.get("readOnlyHint") is not True or ann.get("destructiveHint") is True:
            gaps.append(_gap("allowlisted_but_hints_write"))
        if name in LOOKUP_ARGUMENTS and schema is not None:
            missing = LOOKUP_ARGUMENTS[name] - schema[0]
            if missing:
                gaps.append(_gap("lookup_args_not_in_schema", missing))
            omitted = schema[1] - LOOKUP_ARGUMENTS[name]
            if omitted:
                gaps.append(_gap("lookup_omits_required_args", omitted))
    elif name in KNOWN_WRITE_TOOLS:
        classification, rule, outcome = "write", "known write tool with an interpreter", "evaluated"
    else:
        classification, rule, outcome = ("write", "not in proxy.read_tools and no interpreter (fail-closed)",
                                         "denied")
        gaps.append(_gap("read_hint_not_allowlisted") if looks_read else _gap("write_without_mapping"))
        if looks_read:
            gaps[-1]["basis"] = hint

    if name in ASSUMED_ARGUMENTS:
        accepted, required = ASSUMED_ARGUMENTS[name]
        if schema is None:
            gaps.append(_gap("schema_missing"))
        else:
            props, schema_required, raw_props = schema
            if props - accepted - AGENT_METADATA:
                gaps.append(_gap("schema_fields_not_in_rein", props - accepted - AGENT_METADATA))
            if accepted - props:
                gaps.append(_gap("rein_fields_not_in_schema", accepted - props))
            if schema_required - accepted:
                gaps.append(_gap("schema_requires_unknown_to_rein", schema_required - accepted))
            if required - schema_required:
                gaps.append(_gap("rein_requires_not_required_by_schema", required - schema_required))
            nested = raw_props.get("fields") if name == "ads_update_entity" else None
            if isinstance(nested, dict) and isinstance(nested.get("properties"), dict):
                declared = set(nested["properties"])
                interpreted = set().union(*UPDATE_FIELDS.values())
                if declared - interpreted:
                    gaps.append(_gap("update_fields_not_in_rein", declared - interpreted))
                if interpreted - declared:
                    gaps.append(_gap("rein_update_fields_not_in_schema", interpreted - declared))
    elif schema is None and outcome != "denied":
        gaps.append(_gap("schema_missing"))

    entry = {
        "name": name,
        "rein_classification": classification,
        "classification_rule": rule,
        "proxy_outcome": outcome,
        "interpreter_mapping": name in ASSUMED_ARGUMENTS,
        "agent_metadata": sorted(schema[0] & AGENT_METADATA) if schema else [],
        "annotations": ann,
        "schema": ({"properties": sorted(schema[0]), "required": sorted(schema[1])} if schema else None),
        "gaps": gaps,
    }
    return entry


def build_report(tools: Iterable[types.Tool | dict[str, Any]], policy: Policy, *, downstream: dict[str, Any],
                 server: types.Implementation | dict[str, Any] | None = None) -> dict[str, Any]:
    raw = [t.model_dump(mode="json", exclude_none=True) if isinstance(t, types.Tool) else dict(t) for t in tools]
    names = [t["name"] for t in raw]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    entries = [_tool_entry(t, policy) for t in sorted(raw, key=lambda t: t["name"])]
    if isinstance(server, types.Implementation):
        server = {"name": server.name, "version": server.version}
    offered = set(names)
    gap_counts: dict[str, int] = {}
    for e in entries:
        for g in e["gaps"]:
            gap_counts[g["code"]] = gap_counts.get(g["code"], 0) + 1
    outcomes = {o: sum(e["proxy_outcome"] == o for e in entries) for o in ("passthrough", "evaluated", "denied")}
    return {
        "kind": REPORT_KIND,
        "version": REPORT_VERSION,
        "note": NOTE,
        "downstream": downstream,
        "server": server,
        "summary": {"tools": len(entries), "proxy_outcomes": outcomes, "gaps": dict(sorted(gap_counts.items())),
                    "duplicate_tool_names": duplicates},
        "not_offered": {
            "read_allowlist": sorted(policy.proxy.read_tools - offered),
            "interpreted_writes": sorted(KNOWN_WRITE_TOOLS - offered),
        },
        "tools": entries,
    }


def report_json(report: dict[str, Any], secret: str | None = None) -> str:
    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    return text.replace(secret, REDACTED) if secret else text


def render_summary(report: dict[str, Any]) -> str:
    s = report["summary"]
    o = s["proxy_outcomes"]
    server = report.get("server") or {}
    lines = [
        f"rein proxy discover — {report['downstream'].get('transport')} {report['downstream'].get('endpoint')}"
        + (f"  (server: {server.get('name')} {server.get('version')})" if server else ""),
        f"  tools: {s['tools']}   passthrough: {o['passthrough']}   evaluated: {o['evaluated']}   "
        f"denied: {o['denied']}",
    ]
    for code, count in s["gaps"].items():
        lines.append(f"  gap {code}: {count}")
    for label, missing in (("allowlisted reads not offered", report["not_offered"]["read_allowlist"]),
                           ("interpreted writes not offered", report["not_offered"]["interpreted_writes"])):
        if missing:
            lines.append(f"  {label}: {', '.join(missing)}")
    if s["duplicate_tool_names"]:
        lines.append(f"  duplicate tool names: {', '.join(s['duplicate_tool_names'])}")
    for e in report["tools"]:
        if e["gaps"]:
            lines.append(f"  - {e['name']} [{e['proxy_outcome']}]: " + "; ".join(
                g["code"] + (f" {g['fields']}" if g.get("fields") else "") for g in e["gaps"]))
    lines.append("  " + NOTE)
    return "\n".join(lines)


def tools_from_report(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Rebuild a tools/list from a saved discovery report: names, annotations, property names, required.

    A report records no property types or nested shapes, so every property becomes an empty schema."""
    tools = []
    for t in report["tools"]:
        tool: dict[str, Any] = {"name": t["name"], "annotations": t.get("annotations") or {}}
        if t.get("schema") is not None:
            tool["inputSchema"] = {"type": "object", "properties": {p: {} for p in t["schema"]["properties"]},
                                   "required": list(t["schema"]["required"])}
        tools.append(tool)
    return tools


def load_tools_document(path: str) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """A saved tools/list result ({"tools": [...]}), or a saved discovery report (re-evaluated against
    the current interpreter and policy)."""
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    if isinstance(doc, dict) and doc.get("kind") == REPORT_KIND:
        return tools_from_report(doc), doc.get("server")
    tools = doc.get("tools") if isinstance(doc, dict) else None
    if not isinstance(tools, list) or not all(isinstance(t, dict) and isinstance(t.get("name"), str) for t in tools):
        raise ValueError(f"{path}: expected a tools/list document {{\"tools\": [{{\"name\": ...}}, ...]}}")
    server = doc.get("serverInfo") if isinstance(doc.get("serverInfo"), dict) else None
    return tools, server
