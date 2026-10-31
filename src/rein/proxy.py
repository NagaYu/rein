"""Rein MCP proxy: evaluate an agent's Ads MCP tool calls *before* they run.

    agent (MCP client) --stdio--> Rein proxy --stdio--> Ads MCP (Phase 2: the mock only)

- Tools listed in policy `proxy.read_tools` pass through (and are audited).
- Known write tools are interpreted into the change they would make, evaluated with the
  policy's proxy-scoped rules, and answered with allow / deny / require_approval.
- Anything else is treated as a write we cannot interpret and is denied (fail-closed).

Scope, stated plainly: the proxy governs only calls that go through it. Changes Meta's own AI
makes inside the platform never pass here; those are Phase 1's detect-and-revert territory.
The client identity comes from the MCP `clientInfo` the client sends about itself. It is
recorded as *declared*, never as a verified identity.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Protocol

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from .ledger import ChangeRecord, Ledger
from .mock_ads_mcp import MockAdsMcp, MockToolError
from .policy import Policy, PolicyError, evaluate
from .snapshot import normalize_entity
from .timeutil import iso, now_utc, parse_time

# Write tools whose arguments Rein knows how to turn into a proposed change.
KNOWN_WRITE_TOOLS = frozenset({
    "ads_update_entity", "ads_activate_entity", "ads_create_campaign", "ads_create_ad_set",
    "ads_create_ad", "ads_create_creative",
})
UPDATE_FIELDS = {
    "campaign": frozenset({"name", "status", "daily_budget", "lifetime_budget", "spend_cap"}),
    "adset": frozenset({"name", "status", "daily_budget", "lifetime_budget", "bid_amount", "end_time"}),
    "ad": frozenset({"name", "status", "creative_id"}),
}
BUDGET_FIELDS = frozenset({"daily_budget", "lifetime_budget", "spend_cap", "bid_amount"})
STATUSES = ("ACTIVE", "PAUSED", "ARCHIVED")
ENTITY_KINDS = ("campaign", "adset", "ad")
DECISION_RANK = {"allow": 0, "require_approval": 1, "deny": 2}
NEW_ENTITY_ID = "(new)"

_ID = re.compile(r"^\d{1,20}$")
_ACCOUNT = re.compile(r"^act_\d{1,20}$")


# --------------------------------------------------------------------------- identity / downstream


@dataclass(frozen=True)
class DeclaredClient:
    """What the MCP client says about itself in `initialize`. Not verified."""

    name: str | None = None
    version: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "version": self.version, "source": "mcp.initialize.clientInfo", "verified": False}

    def label(self) -> str:
        return f"{self.name or '?'} {self.version or ''}".strip() + "（申告）"


def declared_client_from(session: Any) -> DeclaredClient:
    params = getattr(session, "client_params", None)
    info = getattr(params, "clientInfo", None) if params else None
    return DeclaredClient(getattr(info, "name", None), getattr(info, "version", None))


@dataclass
class DownstreamResult:
    ok: bool
    structured: dict[str, Any] | None
    text: str

    def to_call_result(self) -> types.CallToolResult:
        return types.CallToolResult(content=[types.TextContent(type="text", text=self.text)],
                                    structuredContent=self.structured, isError=not self.ok)


class Downstream(Protocol):
    async def list_tools(self) -> list[types.Tool]: ...

    async def call(self, name: str, arguments: Any) -> DownstreamResult: ...


class InProcessDownstream:
    """Calls the mock's handlers directly (tests, demo)."""

    def __init__(self, mock: MockAdsMcp):
        self.mock = mock

    async def list_tools(self) -> list[types.Tool]:
        from .mock_ads_mcp import tool_definitions
        return tool_definitions()

    async def call(self, name: str, arguments: Any) -> DownstreamResult:
        try:
            out = self.mock.call(name, arguments)
        except MockToolError as exc:
            return DownstreamResult(False, None, f"error: {exc}")
        return DownstreamResult(True, out, json.dumps(out, ensure_ascii=False))


class SessionDownstream:
    """Calls a downstream MCP server through an MCP client session."""

    def __init__(self, session: ClientSession):
        self.session = session

    async def list_tools(self) -> list[types.Tool]:
        return (await self.session.list_tools()).tools

    async def call(self, name: str, arguments: Any) -> DownstreamResult:
        res = await self.session.call_tool(name, arguments)
        text = "\n".join(c.text for c in res.content if isinstance(c, types.TextContent))
        structured = res.structuredContent
        if structured is None and not res.isError:
            try:
                parsed = json.loads(text)
                structured = parsed if isinstance(parsed, dict) else None
            except ValueError:
                structured = None
        return DownstreamResult(not res.isError, structured, text)


@asynccontextmanager
async def mock_downstream_over_stdio(fixture: str | Path, state: str | Path | None) -> AsyncIterator[SessionDownstream]:
    """Spawn the mock Ads MCP server as a subprocess and talk to it over stdio.

    Phase 2 only ever connects to this mock; there is no option to point the proxy elsewhere."""
    args = ["-m", "rein.mock_ads_mcp", "--fixture", str(fixture)]
    if state:
        args += ["--state", str(state)]
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "PYTHONPATH", "SYSTEMROOT", "LANG")}
    params = StdioServerParameters(command=sys.executable, args=args, env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield SessionDownstream(session)


# --------------------------------------------------------------------------- assessment


class Uninterpretable(Exception):
    pass


class Unresolvable(Exception):
    pass


def _reason(rule_id: str, decision: str, reason: str, rule_type: str = "rein") -> dict[str, Any]:
    return {"rule_id": rule_id, "rule_type": rule_type, "decision": decision, "reason": reason}


CURRENT_UNAVAILABLE = _reason(
    "rein:current-unavailable", "require_approval",
    "下流から現在値を取得できないため、許可せず承認待ちにする（フェイルクローズ）")


@dataclass
class Proposal:
    entity_type: str | None
    entity_id: str | None
    changes: list[ChangeRecord] = field(default_factory=list)
    precheck: dict[str, Any] = field(default_factory=dict)  # values the call was judged against
    notes: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Assessment:
    tool: str
    kind: str  # read | write | unknown
    decision: str  # passthrough | allow | deny | require_approval
    reasons: list[dict[str, Any]] = field(default_factory=list)
    entity_type: str | None = None
    entity_id: str | None = None
    precheck: dict[str, Any] = field(default_factory=dict)
    proposed: list[dict[str, Any]] = field(default_factory=list)


def _check_keys(args: Any, allowed: set[str], required: set[str]) -> dict[str, Any]:
    if not isinstance(args, dict):
        raise Uninterpretable("引数がオブジェクトではない")
    unknown = set(args) - allowed
    if unknown:
        raise Uninterpretable(f"未対応の引数 {sorted(unknown)}")
    missing = required - set(args)
    if missing:
        raise Uninterpretable(f"必須引数が無い {sorted(missing)}")
    return args


def _id(value: Any, what: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not _ID.match(str(value)):
        raise Uninterpretable(f"{what} が ID として解釈できない: {value!r}")
    return str(value)


def _budget(value: Any, what: str) -> int:
    if isinstance(value, bool):
        raise Uninterpretable(f"{what} が金額として解釈できない: {value!r}")
    if isinstance(value, int):
        n = value
    elif isinstance(value, str) and value.strip().isdigit():
        n = int(value.strip())
    else:
        raise Uninterpretable(f"{what} が金額として解釈できない: {value!r}")
    if n <= 0:
        raise Uninterpretable(f"{what} は正の整数である必要がある: {value!r}")
    return n


def _text(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise Uninterpretable(f"{what} が空、または文字列ではない")
    return value


def _account(value: Any) -> str:
    if not isinstance(value, str) or not _ACCOUNT.match(value):
        raise Uninterpretable(f"account_id が act_<数字> の形ではない: {value!r}")
    return value


class CallAssessor:
    def __init__(self, policy: Policy, downstream: Downstream, now: datetime):
        self.policy = policy
        self.downstream = downstream
        self.now = now

    # ---------------------------------------------------------------- downstream reads

    async def _fetch(self, entity_type: str, entity_id: str) -> tuple[str, dict[str, Any] | None]:
        """Returns (found | not_found | unavailable, row)."""
        if entity_type == "creative":
            tool, args = "ads_get_creatives", {"ids": [entity_id]}
        else:
            tool, args = "ads_get_ad_entities", {"entity_type": entity_type, "ids": [entity_id]}
        try:
            res = await self.downstream.call(tool, args)
        except Exception:
            return "unavailable", None
        data = (res.structured or {}).get("data") if res.ok else None
        if not isinstance(data, list):
            return "unavailable", None
        rows = [r for r in data if isinstance(r, dict) and str(r.get("id")) == entity_id]
        return ("found", rows[0]) if rows else ("not_found", None)

    async def _locate(self, entity_id: str, entity_type: str | None) -> tuple[str, str | None, dict[str, Any] | None]:
        unavailable = False
        for t in ([entity_type] if entity_type else list(ENTITY_KINDS)):
            status, row = await self._fetch(t, entity_id)
            if status == "found":
                return "found", t, row
            unavailable |= status == "unavailable"
        return ("unavailable" if unavailable else "not_found"), entity_type, None

    def _change(self, entity_type: str | None, entity_id: str | None, field_path: str, before: Any, after: Any,
                change_type: str | None = None, name: str | None = None) -> ChangeRecord:
        if change_type is None:
            change_type = "added" if before is None else ("removed" if after is None else "modified")
        return ChangeRecord(
            id=0, detected_at=self.now, account_id="", from_snapshot_id=0, to_snapshot_id=0,
            entity_type=entity_type or "unknown", entity_id=entity_id or "", entity_name=name, field=field_path,
            change_type=change_type, before=before, after=after, entity_updated_time=None, actor_kind="unknown",
            attribution={"basis": "proxy_call", "requested_at": iso(self.now)},
            evaluated_at=None, violation=False, severity=None, findings=[],
        )

    # ---------------------------------------------------------------- entry point

    async def assess(self, tool: str, args: Any) -> Assessment:
        if tool in self.policy.proxy.read_tools:
            return Assessment(tool, "read", "passthrough")
        if tool not in KNOWN_WRITE_TOOLS:
            return Assessment(tool, "unknown", "deny", [_reason(
                "rein:unknown-tool", "deny",
                f"{tool} は読み取り系の許可リストに無く、変更内容を解釈できないため拒否（フェイルクローズ）")])
        try:
            proposal = await getattr(self, f"_interpret_{tool}")(args)
        except Uninterpretable as exc:
            return Assessment(tool, "write", "deny", [_reason("rein:uninterpretable", "deny", f"解釈不能: {exc}")])
        except Unresolvable as exc:
            return Assessment(tool, "write", "deny", [_reason("rein:unresolvable", "deny", f"対象エンティティを特定できない: {exc}")])

        reasons = list(proposal.notes)
        for change in proposal.changes:
            for f in evaluate(change, self.policy, scope="proxy").findings:
                reasons.append(_reason(f.rule_id, f.on_proxy, f.reason, f.rule_type))
        unique = []
        for r in reasons:
            if r not in unique:
                unique.append(r)
        decision = max((r["decision"] for r in unique), key=DECISION_RANK.__getitem__, default="allow")
        return Assessment(tool, "write", decision, unique, proposal.entity_type, proposal.entity_id, proposal.precheck,
                          [{"entity_type": c.entity_type, "entity_id": c.entity_id, "field": c.field,
                            "change_type": c.change_type, "before": c.before, "after": c.after}
                           for c in proposal.changes])

    # ---------------------------------------------------------------- updates

    async def _interpret_ads_update_entity(self, args: Any) -> Proposal:
        args = _check_keys(args, {"entity_id", "entity_type", "fields", "account_id"}, {"entity_id", "fields"})
        entity_id = _id(args["entity_id"], "entity_id")
        given_type = args.get("entity_type")
        if given_type is not None and given_type not in ENTITY_KINDS:
            raise Uninterpretable(f"entity_type が不正: {given_type!r}")
        fields = args["fields"]
        if not isinstance(fields, dict) or not fields:
            raise Uninterpretable("fields が空、またはオブジェクトではない")
        status, etype, row = await self._locate(entity_id, given_type)
        if status == "not_found":
            raise Unresolvable(f"{entity_id} が下流に存在しない")
        allowed = UPDATE_FIELDS[etype] if etype else frozenset().union(*UPDATE_FIELDS.values())
        unknown = set(fields) - allowed
        if unknown:
            raise Uninterpretable(f"{etype or 'entity'} で扱えないフィールド {sorted(unknown)}")
        parsed: dict[str, Any] = {}
        for key, value in fields.items():
            if key in BUDGET_FIELDS:
                parsed[key] = _budget(value, key)
            elif key == "status":
                if value not in STATUSES:
                    raise Uninterpretable(f"status が不正: {value!r}")
                parsed[key] = value
            elif key == "creative_id":
                parsed[key] = _id(value, "creative_id")
            else:
                parsed[key] = _text(value, key)
        return await self._update_proposal(etype, entity_id, row, parsed)

    async def _update_proposal(self, etype: str | None, entity_id: str, row: dict[str, Any] | None,
                               parsed: dict[str, Any]) -> Proposal:
        current = normalize_entity(etype, row) if row is not None and etype else None
        name = current.get("name") if current else None
        proposal = Proposal(etype, entity_id)
        if current is None:
            proposal.notes.append(CURRENT_UNAVAILABLE)
        for key, value in parsed.items():
            if key == "creative_id":
                await self._creative_swap(proposal, current, value, name)
                continue
            before = current.get(key) if current else None
            proposal.changes.append(self._change(etype, entity_id, key, before, value, name=name))
            if current is not None:
                proposal.precheck[key] = before
        return proposal

    async def _creative_swap(self, proposal: Proposal, current: dict[str, Any] | None, new_id: str,
                             name: str | None) -> None:
        old_id = (current.get("creative") or {}).get("id") if current else None
        proposal.changes.append(self._change("ad", proposal.entity_id, "creative.id", old_id, new_id, name=name))
        if current is not None:
            proposal.precheck["creative.id"] = old_id
        status, new_row = await self._fetch("creative", new_id)
        if status == "not_found":
            raise Unresolvable(f"付け替え先のクリエイティブ {new_id} が存在しない")
        old_row = None
        if old_id:
            old_status, old_row = await self._fetch("creative", old_id)
        if status != "found" or old_row is None:
            # Without both sides we cannot tell whether the link/text changes: do not allow.
            if CURRENT_UNAVAILABLE not in proposal.notes:
                proposal.notes.append(CURRENT_UNAVAILABLE)
            return
        old_res = normalize_entity("creative", old_row).get("resolved", {})
        new_res = normalize_entity("creative", new_row).get("resolved", {})
        for key in sorted(old_res.keys() | new_res.keys()):
            if old_res.get(key) != new_res.get(key):
                proposal.changes.append(self._change("ad", proposal.entity_id, f"creative_resolved.{key}",
                                                     old_res.get(key), new_res.get(key), name=name))

    async def _interpret_ads_activate_entity(self, args: Any) -> Proposal:
        args = _check_keys(args, {"entity_id", "entity_type", "account_id"}, {"entity_id"})
        entity_id = _id(args["entity_id"], "entity_id")
        given_type = args.get("entity_type")
        if given_type is not None and given_type not in ENTITY_KINDS:
            raise Uninterpretable(f"entity_type が不正: {given_type!r}")
        status, etype, row = await self._locate(entity_id, given_type)
        if status == "not_found":
            raise Unresolvable(f"{entity_id} が下流に存在しない")
        return await self._update_proposal(etype, entity_id, row, {"status": "ACTIVE"})

    # ---------------------------------------------------------------- creates

    def _created(self, entity_type: str, raw: dict[str, Any]) -> Proposal:
        after = normalize_entity(entity_type, {"id": NEW_ENTITY_ID, **raw})
        proposal = Proposal(entity_type, None)
        proposal.changes.append(self._change(entity_type, NEW_ENTITY_ID, "*", None, after, "entity_created",
                                             name=after.get("name")))
        return proposal

    async def _interpret_ads_create_campaign(self, args: Any) -> Proposal:
        args = _check_keys(args, {"account_id", "name", "objective", "daily_budget", "lifetime_budget",
                                  "special_ad_categories"}, {"account_id", "name", "objective"})
        _account(args["account_id"])
        raw: dict[str, Any] = {"name": _text(args["name"], "name"), "objective": _text(args["objective"], "objective"),
                               "status": "PAUSED"}
        for key in ("daily_budget", "lifetime_budget"):
            if key in args:
                raw[key] = _budget(args[key], key)
        if "special_ad_categories" in args:
            if not isinstance(args["special_ad_categories"], list):
                raise Uninterpretable("special_ad_categories がリストではない")
            raw["special_ad_categories"] = args["special_ad_categories"]
        return self._created("campaign", raw)

    async def _interpret_ads_create_ad_set(self, args: Any) -> Proposal:
        args = _check_keys(args, {"account_id", "campaign_id", "name", "daily_budget", "lifetime_budget",
                                  "optimization_goal", "billing_event", "targeting", "start_time", "end_time"},
                           {"account_id", "campaign_id", "name", "optimization_goal", "billing_event"})
        _account(args["account_id"])
        campaign_id = _id(args["campaign_id"], "campaign_id")
        raw: dict[str, Any] = {"campaign_id": campaign_id, "status": "PAUSED"}
        for key in ("name", "optimization_goal", "billing_event"):
            raw[key] = _text(args[key], key)
        for key in ("daily_budget", "lifetime_budget"):
            if key in args:
                raw[key] = _budget(args[key], key)
        if "targeting" in args:
            if not isinstance(args["targeting"], dict):
                raise Uninterpretable("targeting がオブジェクトではない")
            raw["targeting"] = args["targeting"]
        for key in ("start_time", "end_time"):
            if key in args:
                try:
                    parse_time(_text(args[key], key))
                except ValueError as exc:
                    raise Uninterpretable(f"{key} が日時として解釈できない") from exc
                raw[key] = args[key]
        status, _ = await self._fetch("campaign", campaign_id)
        if status == "not_found":
            raise Unresolvable(f"親キャンペーン {campaign_id} が存在しない")
        proposal = self._created("adset", raw)
        if status == "unavailable":
            proposal.notes.append(CURRENT_UNAVAILABLE)
        return proposal

    async def _interpret_ads_create_ad(self, args: Any) -> Proposal:
        args = _check_keys(args, {"account_id", "adset_id", "name", "creative_id"},
                           {"account_id", "adset_id", "name", "creative_id"})
        _account(args["account_id"])
        adset_id = _id(args["adset_id"], "adset_id")
        creative_id = _id(args["creative_id"], "creative_id")
        raw: dict[str, Any] = {"name": _text(args["name"], "name"), "adset_id": adset_id, "status": "PAUSED",
                               "creative": {"id": creative_id}}
        adset_status, _ = await self._fetch("adset", adset_id)
        if adset_status == "not_found":
            raise Unresolvable(f"親の広告セット {adset_id} が存在しない")
        cr_status, cr_row = await self._fetch("creative", creative_id)
        if cr_status == "not_found":
            raise Unresolvable(f"クリエイティブ {creative_id} が存在しない")
        if cr_row is not None:
            # Expose the creative's link/text on the new ad, as snapshots do, so text rules apply.
            raw["creative_resolved"] = normalize_entity("creative", cr_row).get("resolved")
        proposal = self._created("ad", raw)
        if "unavailable" in (adset_status, cr_status):
            proposal.notes.append(CURRENT_UNAVAILABLE)
        return proposal

    async def _interpret_ads_create_creative(self, args: Any) -> Proposal:
        args = _check_keys(args, {"account_id", "name", "page_id", "link", "message", "headline", "description",
                                  "call_to_action"}, {"account_id", "name", "link", "message"})
        _account(args["account_id"])
        link = _text(args["link"], "link")
        if not link.startswith(("https://", "http://")):
            raise Uninterpretable(f"link が URL として解釈できない: {link!r}")
        link_data: dict[str, Any] = {"link": link, "message": _text(args["message"], "message")}
        for src, dst in (("headline", "name"), ("description", "description")):
            if src in args:
                link_data[dst] = _text(args[src], src)
        if "call_to_action" in args:
            link_data["call_to_action"] = {"type": _text(args["call_to_action"], "call_to_action")}
        raw = {"name": _text(args["name"], "name"), "object_story_spec": {"link_data": link_data}}
        if "page_id" in args:
            raw["object_story_spec"]["page_id"] = _id(args["page_id"], "page_id")
        return self._created("creative", raw)


# --------------------------------------------------------------------------- proxy


def summarize_arguments(arguments: Any, limit: int = 240) -> tuple[str, str]:
    canonical = json.dumps(arguments, ensure_ascii=False, sort_keys=True, default=str)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return (canonical if len(canonical) <= limit else canonical[: limit - 1] + "…"), digest


def _short(text: str, limit: int = 240) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def rein_result(payload: dict[str, Any], text: str) -> types.CallToolResult:
    """Rein's own answer (deny / pending approval). isError=True: the requested change did NOT happen."""
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)],
                                structuredContent={"rein": payload}, isError=True)


@dataclass
class ApprovalOutcome:
    approval_id: str
    status: str
    executed: bool
    message: str
    result: dict[str, Any] | None = None
    changed: bool = False  # whether this call moved the approval to a new status


class ApprovalError(KeyError):
    pass


class ReinProxy:
    def __init__(self, policy: Policy, ledger: Ledger, downstream: Downstream, *,
                 clock: Callable[[], datetime] = now_utc):
        overlap = policy.proxy.read_tools & KNOWN_WRITE_TOOLS
        if overlap:
            raise PolicyError(f"proxy.read_tools must not contain write tools: {sorted(overlap)}")
        self.policy = policy
        self.ledger = ledger
        self.downstream = downstream
        self.clock = clock

    # ---------------------------------------------------------------- calls

    async def _forward(self, tool: str, arguments: Any) -> DownstreamResult:
        try:
            return await self.downstream.call(tool, arguments)
        except Exception as exc:  # a broken downstream is reported, never retried
            return DownstreamResult(False, None, f"downstream error: {type(exc).__name__}")

    async def handle(self, tool: str, arguments: Any, client: DeclaredClient) -> types.CallToolResult:
        now = self.clock()
        assessment = await CallAssessor(self.policy, self.downstream, now).assess(tool, arguments)
        summary, digest = summarize_arguments(arguments)
        audit = dict(occurred_at=iso(now), event="call", declared_client=client.to_dict(), tool=tool,
                     arguments_summary=summary, arguments_sha256=digest, reasons=assessment.reasons,
                     entity_type=assessment.entity_type, entity_id=assessment.entity_id)
        if assessment.kind == "read":
            audit.update(entity_type=(arguments or {}).get("entity_type") if isinstance(arguments, dict) else None)

        if assessment.decision in ("passthrough", "allow"):
            res = await self._forward(tool, arguments)
            if assessment.entity_id is None and assessment.kind == "write" and res.ok:
                audit["entity_id"] = str((res.structured or {}).get("id") or "") or None
            self.ledger.record_audit(**audit, decision=assessment.decision, forwarded=1,
                                     downstream_ok=int(res.ok), result_summary=_short(res.text))
            return res.to_call_result()

        if assessment.decision == "deny":
            audit_id = self.ledger.record_audit(**audit, decision="deny", forwarded=0)
            payload = {"decision": "deny", "executed": False, "reasons": assessment.reasons, "audit_id": audit_id}
            lines = [f"Rein: 拒否しました（実行していません）。{tool}"] + [f"- {r['rule_id']}: {r['reason']}" for r in assessment.reasons]
            return rein_result(payload, "\n".join(lines))

        approval_id = "apr_" + secrets.token_hex(6)
        expires = now + timedelta(seconds=int(self.policy.proxy.approval_ttl_hours * 3600))
        self.ledger.insert_approval(
            id=approval_id, received_at=iso(now), expires_at=iso(expires), declared_client=client.to_dict(),
            tool=tool, arguments=arguments, findings=assessment.reasons, precheck=assessment.precheck,
            entity_type=assessment.entity_type, entity_id=assessment.entity_id, status="pending")
        audit_id = self.ledger.record_audit(**audit, decision="require_approval", approval_id=approval_id, forwarded=0)
        payload = {"decision": "require_approval", "executed": False, "approval_id": approval_id, "status": "pending",
                   "expires_at": iso(expires), "reasons": assessment.reasons, "audit_id": audit_id}
        lines = [f"Rein: 承認待ちにしました（まだ実行していません）。approval_id={approval_id} 期限={iso(expires)}"]
        lines += [f"- {r['rule_id']}: {r['reason']}" for r in assessment.reasons]
        return rein_result(payload, "\n".join(lines))

    # ---------------------------------------------------------------- approvals

    def _audit_approval(self, approval: dict[str, Any], event: str, decision: str, operator: str | None,
                        reasons: list[dict[str, Any]], **extra: Any) -> None:
        summary, digest = summarize_arguments(approval["arguments"])
        self.ledger.record_audit(
            occurred_at=iso(self.clock()), event=event, declared_client=approval["declared_client"], operator=operator,
            tool=approval["tool"], arguments_summary=summary, arguments_sha256=digest, decision=decision,
            reasons=reasons, entity_type=approval["entity_type"], entity_id=extra.pop("entity_id", approval["entity_id"]),
            approval_id=approval["id"], **extra)

    def _get(self, approval_id: str) -> dict[str, Any]:
        approval = self.ledger.get_approval(approval_id)
        if approval is None:
            raise ApprovalError(f"approval {approval_id} not found")
        return approval

    def expire_due(self) -> list[str]:
        """Mark every pending approval whose deadline has passed (now >= expires_at) as expired."""
        now = iso(self.clock())
        expired = []
        for approval in self.ledger.list_approvals("pending"):
            if now >= approval["expires_at"] and self.ledger.transition_approval(
                    approval["id"], "pending", "expired", status_reason=f"期限 {approval['expires_at']} を過ぎた"):
                self._audit_approval(approval, "approval_expired", "expired", None,
                                     [_reason("rein:approval-expired", "deny", f"承認期限 {approval['expires_at']} を過ぎた")])
                expired.append(approval["id"])
        return expired

    def reject(self, approval_id: str, operator: str, note: str | None = None) -> ApprovalOutcome:
        approval = self._get(approval_id)
        self.expire_due()
        if not self.ledger.transition_approval(approval_id, "pending", "rejected", decided_at=iso(self.clock()),
                                               decided_by=operator, status_reason=note or "却下"):
            current = self._get(approval_id)
            return ApprovalOutcome(approval_id, current["status"], False, f"pending ではないため却下できない（現在: {current['status']}）")
        self._audit_approval(approval, "approval_rejected", "rejected", operator,
                             [_reason("rein:approval-rejected", "deny", note or "承認者が却下した")])
        return ApprovalOutcome(approval_id, "rejected", False, "却下した。このコールは今後も実行されない", changed=True)

    async def approve(self, approval_id: str, operator: str) -> ApprovalOutcome:
        approval = self._get(approval_id)
        self.expire_due()
        now = self.clock()
        claimed = self.ledger.transition_approval(approval_id, "pending", "approved", unexpired_at=iso(now),
                                                  decided_at=iso(now), decided_by=operator)
        if not claimed:
            current = self._get(approval_id)
            msg = {
                "executed": "既に実行済み。再実行しない",
                "expired": "承認期限切れのため実行できない",
                "rejected": "却下済みのため実行できない",
                "aborted": "再確認で中止済み。再実行しない",
                "failed": "実行を試みて失敗済み。自動では再実行しない",
                "approved": "既に承認処理中、または処理が中断された。再実行しない",
            }.get(current["status"], f"状態 {current['status']} のため実行できない")
            self._audit_approval(current, "approval_refused", "refused", operator,
                                 [_reason("rein:approval-not-pending", "deny", msg)])
            return ApprovalOutcome(approval_id, current["status"], False, msg)

        # Re-check right before running: the world may have moved while the call waited.
        recheck = await CallAssessor(self.policy, self.downstream, now).assess(approval["tool"], approval["arguments"])
        problems = [r for r in recheck.reasons if r["decision"] == "deny"]
        if CURRENT_UNAVAILABLE in recheck.reasons:
            problems.append(_reason("rein:recheck-unavailable", "deny", "実行直前の再確認で現在値を取得できない"))
        for key, old in (approval["precheck"] or {}).items():
            new = recheck.precheck.get(key)
            if key in recheck.precheck and new != old:
                problems.append(_reason("rein:recheck-drift", "deny",
                                        f"承認待ちの間に {key} が {old!r} から {new!r} に変わった"))
        if problems:
            text = " / ".join(p["reason"] for p in problems)
            self.ledger.transition_approval(approval_id, "approved", "aborted", status_reason=f"再確認で中止: {text}")
            self._audit_approval(approval, "approval_aborted", "aborted", operator, problems)
            return ApprovalOutcome(approval_id, "aborted", False, f"再確認で状況が変わっていたため実行しない: {text}",
                                   changed=True)

        res = await self._forward(approval["tool"], approval["arguments"])
        status = "executed" if res.ok else "failed"
        result = {"ok": res.ok, "structured": res.structured, "text": _short(res.text, 1000)}
        self.ledger.transition_approval(approval_id, "approved", status, executed_at=iso(self.clock()), result=result,
                                        status_reason=None if res.ok else "下流がエラーを返した")
        entity_id = approval["entity_id"] or (str((res.structured or {}).get("id") or "") or None)
        self._audit_approval(approval, f"approval_{status}", status, operator, approval["findings"], forwarded=1,
                             downstream_ok=int(res.ok), result_summary=_short(res.text), entity_id=entity_id)
        return ApprovalOutcome(approval_id, status, res.ok,
                               "承認により 1 回実行した" if res.ok else f"下流がエラーを返した: {_short(res.text)}", result,
                               changed=True)


# --------------------------------------------------------------------------- MCP server facade


def build_proxy_server(proxy: ReinProxy) -> Server:
    server: Server = Server(
        "rein-proxy", version="0.2.0",
        instructions="Rein governs write calls to the Ads MCP before they run. Denied or pending calls are "
                     "returned as errors with a `rein` object explaining why; they were not executed.")

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:
        try:
            return await proxy.downstream.list_tools()
        except Exception:
            return []

    # Input validation stays off: malformed calls must reach Rein so they are judged and audited.
    @server.call_tool(validate_input=False)
    async def _call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        client = declared_client_from(server.request_context.session)
        return await proxy.handle(name, arguments, client)

    return server


async def serve_stdio(policy: Policy, db: str, fixture: str, state: str | None) -> None:
    async with mock_downstream_over_stdio(fixture, state) as downstream:
        proxy = ReinProxy(policy, Ledger(db), downstream)
        server = build_proxy_server(proxy)
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
