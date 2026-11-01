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

from .downstream_http import DownstreamFailure
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
# Argument shapes, refitted to the real Ads MCP tools/list (discover-live.json, Phase 5C). That report
# records property names and required lists only, so types, enum values and nested shapes are not known;
# where they matter Rein interprets conservatively and otherwise fails closed.
ACCOUNT_ARG = "ad_account_id"
# Present on nearly every real tool: the agent's own bookkeeping. Accepted and forwarded unchanged,
# kept in the audit's argument summary/hash like any argument, never used for a decision.
AGENT_METADATA = frozenset({"advertiser_job", "advertiser_request", "client_conversation_id", "scheduled_task_run"})
# Optional create arguments Rein accepts without interpreting: only ones that cannot change spend,
# delivery, targeting, destination or content. Every other argument outside the interpreted set makes
# the call uninterpretable (denied), even when the downstream schema declares it.
BENIGN_OPTIONALS = frozenset({"adlabels"})
# Per write tool: (arguments Rein interprets or accepts, arguments Rein requires). Matches the real
# required lists; `rein proxy discover` compares this table with a live tools/list.
ASSUMED_ARGUMENTS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "ads_update_entity": (frozenset({ACCOUNT_ARG, "entity_id", "entity_type", "fields"}),
                          frozenset({ACCOUNT_ARG, "entity_id", "entity_type"})),
    # publish_as_active / allow_partial_publish: Phase 7, literal booleans only (see the interpreter).
    "ads_activate_entity": (frozenset({ACCOUNT_ARG, "entity_id", "entity_type", "publish_as_active",
                                       "allow_partial_publish"}),
                            frozenset({ACCOUNT_ARG, "entity_id", "entity_type"})),
    "ads_create_campaign": (frozenset({ACCOUNT_ARG, "campaign_name", "objective", "buying_type",
                                       "special_ad_categories", "special_ad_category_country",
                                       "campaign_daily_budget", "campaign_lifetime_budget", "campaign_spend_cap",
                                       "campaign_start_time", "campaign_stop_time"}) | BENIGN_OPTIONALS,
                            frozenset({ACCOUNT_ARG, "buying_type", "campaign_name", "objective"})),
    "ads_create_ad_set": (frozenset({ACCOUNT_ARG, "campaign_id", "ad_set_name", "optimization_goal", "billing_event",
                                     "targeting", "daily_budget", "lifetime_budget", "bid_amount", "start_time",
                                     "end_time"}) | BENIGN_OPTIONALS,
                          frozenset({ACCOUNT_ARG, "ad_set_name", "billing_event", "campaign_id", "optimization_goal",
                                     "targeting"})),
    "ads_create_ad": (frozenset({ACCOUNT_ARG, "ad_set_id", "ad_name", "creative"}) | BENIGN_OPTIONALS,
                      frozenset({ACCOUNT_ARG, "ad_name", "ad_set_id"})),
    "ads_create_creative": (frozenset({ACCOUNT_ARG, "name", "page_id", "link_url", "message", "headline",
                                       "description", "call_to_action_type", "primary_texts", "headlines",
                                       "descriptions", "image_hash"}),
                            frozenset({ACCOUNT_ARG})),
}
# Arguments Rein sends when it reads current values. `level` is not sent: the report does not give its
# allowed values, and the entity type is already known from the call that triggers the lookup.
LOOKUP_ARGUMENTS: dict[str, frozenset[str]] = {
    "ads_get_ad_entities": frozenset({ACCOUNT_ARG, "object_ids", "fields"}),
    "ads_get_creatives": frozenset({ACCOUNT_ARG, "creative_ids", "fields"}),
}
LOOKUP_FIELDS = {
    "campaign": ("id", "name", "status", "daily_budget", "lifetime_budget", "spend_cap"),
    "adset": ("id", "name", "status", "campaign_id", "daily_budget", "lifetime_budget", "bid_amount", "end_time"),
    "ad": ("id", "name", "status", "adset_id", "creative"),
    "creative": ("id", "name", "object_story_spec", "asset_feed_spec"),
}
# Spellings of entity_type Rein reads unambiguously (the real enum values are not in the report).
ENTITY_TYPE_ALIASES = {"campaign": "campaign", "adset": "adset", "ad_set": "adset", "ad": "ad"}
STATUSES = ("ACTIVE", "PAUSED", "ARCHIVED")
ENTITY_KINDS = ("campaign", "adset", "ad")
DECISION_RANK = {"allow": 0, "require_approval": 1, "deny": 2}
NEW_ENTITY_ID = "(new)"

_ID = re.compile(r"^\d{1,20}$")
_ACCOUNT = re.compile(r"^(act_)?\d{1,20}$")


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
    detail: str | None = None        # bounded, single-line, token-scrubbed summary of what the server said
    failure: str | None = None       # transport failure class: the call may never have reached the server
    content_types: tuple[str, ...] = ()

    def to_call_result(self) -> types.CallToolResult:
        structured = self.structured
        if not self.ok and self.detail is not None and structured is None:
            structured = {"rein_downstream": {"failure": self.failure, "detail": self.detail}}
        return types.CallToolResult(content=[types.TextContent(type="text", text=self.text)],
                                    structuredContent=structured, isError=not self.ok)


def describe_result(res: DownstreamResult) -> str | None:
    """What the downstream said, as one bounded line: its text, else its structured content, else a note."""
    from .downstream_http import sanitize_detail

    if res.failure:
        return res.detail
    if res.text and res.text.strip():
        return sanitize_detail(res.text)
    if res.structured:
        return sanitize_detail(res.structured)
    kinds = ", ".join(res.content_types)
    return f"(no text or structured content in the response{'; content types: ' + kinds if kinds else ''})"


def is_draft(res: DownstreamResult) -> bool:
    """A create answer whose top-level `status` is exactly "DRAFT". Uses the full answer, not the 500-character
    detail (a real draft descriptor is longer than that). Anything unparseable or any other status: False."""
    obj = res.structured if isinstance(res.structured, dict) else None
    if obj is None and res.text:
        try:
            parsed = json.loads(res.text)
        except ValueError:
            parsed = None
        obj = parsed if isinstance(parsed, dict) else None
    return obj is not None and obj.get("status") == "DRAFT"


_ERROR_TEXT = re.compile(r"^\s*(error|failed|failure)\b\s*[:\-(]", re.IGNORECASE)


def error_content(res: DownstreamResult) -> str | None:
    """Why a transport-successful result is really an error, or None. Deliberately narrow:

    - the result is flagged isError;
    - its structured content, or its text parsed as JSON, is an object whose `error` / `errors` is non-empty,
      or whose `success` is exactly false;
    - its text starts with "error:", "failed:" or "failure:" (or "-"/"(" after the word).
    Normal success payloads (ids, status fields, `success: true`) never match."""
    if not res.ok:
        return "the result is flagged isError"
    candidates = [res.structured] if isinstance(res.structured, dict) else []
    try:
        parsed = json.loads(res.text) if res.text else None
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        candidates.append(parsed)
    for obj in candidates:
        if obj.get("error") not in (None, "", {}, [], False):
            return "the result content carries an `error` field"
        if obj.get("errors") not in (None, "", {}, [], False):
            return "the result content carries an `errors` field"
        if obj.get("success") is False:
            return "the result content says success: false"
    if parsed is None and res.text and _ERROR_TEXT.match(res.text):
        return "the result text is an error message"
    return None


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
        self.server_info: types.Implementation | None = None

    async def list_tools(self) -> list[types.Tool]:
        return (await self.session.list_tools()).tools

    async def call(self, name: str, arguments: Any) -> DownstreamResult:
        res = await self.session.call_tool(name, arguments)
        texts = []
        for c in res.content:
            if isinstance(c, types.TextContent):
                texts.append(c.text)
            elif isinstance(c, types.EmbeddedResource) and isinstance(getattr(c.resource, "text", None), str):
                texts.append(c.resource.text)
        text = "\n".join(texts)
        structured = res.structuredContent
        if structured is None and not res.isError:
            try:
                parsed = json.loads(text)
                structured = parsed if isinstance(parsed, dict) else None
            except ValueError:
                structured = None
        return DownstreamResult(not res.isError, structured, text,
                                content_types=tuple(c.type for c in res.content))


@asynccontextmanager
async def mock_downstream_over_stdio(fixture: str | Path, state: str | Path | None, *,
                                     session_class: type[ClientSession] = ClientSession
                                     ) -> AsyncIterator[SessionDownstream]:
    """Spawn the mock Ads MCP server as a subprocess and talk to it over stdio.

    Phase 2 only ever connects to this mock; there is no option to point the proxy elsewhere."""
    args = ["-m", "rein.mock_ads_mcp", "--fixture", str(fixture)]
    if state:
        args += ["--state", str(state)]
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "PYTHONPATH", "SYSTEMROOT", "LANG")}
    params = StdioServerParameters(command=sys.executable, args=args, env=env)
    async with stdio_client(params) as (read, write):
        async with session_class(read, write) as session:
            info = (await session.initialize()).serverInfo
            downstream = SessionDownstream(session)
            downstream.server_info = info
            yield downstream


# --------------------------------------------------------------------------- assessment


class Uninterpretable(Exception):
    pass


class Unresolvable(Exception):
    pass


def _failure_reason(label: str, detail: str | None, decision: str = "require_approval",
                    what: str = "下流の読み取りに失敗") -> dict[str, Any]:
    reason = _reason("rein:downstream-failure", decision, f"{what}: {label}")
    if detail:
        reason["detail"] = detail  # diagnostic only, bounded by sanitize_detail
    return reason


def _reason(rule_id: str, decision: str, reason: str, rule_type: str = "rein") -> dict[str, Any]:
    return {"rule_id": rule_id, "rule_type": rule_type, "decision": decision, "reason": reason}


CURRENT_UNAVAILABLE = _reason(
    "rein:current-unavailable", "require_approval",
    "下流から現在値を取得できないため、許可せず承認待ちにする（フェイルクローズ）")
NO_FIELD_CHANGES = _reason(
    "rein:no-field-changes", "require_approval",
    "fields の無い更新: 判定できる変更が無く、下流が何をするかを Rein は確認できないため承認待ちにする")
PUBLISH_NEEDS_APPROVAL = _reason(
    "publish-needs-approval", "require_approval",
    "publish_as_active: true はドラフトを公開して実体化する呼び出しのため、他のルールの結果にかかわらず常に人の承認を必要とする")


def _target_not_entity(entity_id: str, info: str) -> dict[str, Any]:
    reason = _reason("rein:target-not-entity", "require_approval",
                     f"下流が {entity_id} をアカウントのエンティティではないと答えた（ドラフト等の可能性）。"
                     "現在値を確認できないため承認待ちにする")
    reason["detail"] = info
    return reason


CREATIVE_NOT_RESOLVED = _reason(
    "rein:creative-not-resolved", "require_approval",
    "広告のクリエイティブを既存クリエイティブの参照（creative.creative_id）として読めないため、"
    "リンク先と文面を確認できず承認待ちにする")


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


def _check_keys(args: Any, allowed: frozenset[str], required: frozenset[str]) -> dict[str, Any]:
    """Drop agent metadata, then insist every remaining argument is one Rein interprets or accepts."""
    if not isinstance(args, dict):
        raise Uninterpretable("引数がオブジェクトではない")
    args = {k: v for k, v in args.items() if k not in AGENT_METADATA}
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


def _time(value: Any, what: str) -> str:
    try:
        parse_time(_text(value, what))
    except ValueError as exc:
        raise Uninterpretable(f"{what} が日時として解釈できない") from exc
    return value


def _account(value: Any) -> str:
    """ad_account_id as sent ("act_<digits>" or "<digits>") -> the internal account id "act_<digits>"."""
    if not isinstance(value, str) or not _ACCOUNT.match(value):
        raise Uninterpretable(f"ad_account_id が act_<数字> または数字の形ではない: {value!r}")
    return value if value.startswith("act_") else f"act_{value}"


def _entity_type(value: Any) -> str:
    etype = ENTITY_TYPE_ALIASES.get(value.lower()) if isinstance(value, str) else None
    if etype is None:
        raise Uninterpretable(f"entity_type が不正: {value!r}")
    return etype


def _texts_arg(args: dict[str, Any], single: str, plural: str) -> list[str]:
    values = [_text(args[single], single)] if single in args else []
    if plural in args:
        items = args[plural]
        if not isinstance(items, list) or not items:
            raise Uninterpretable(f"{plural} が空、またはリストではない")
        values += [_text(v, plural) for v in items]
    return list(dict.fromkeys(values))


class CallAssessor:
    def __init__(self, policy: Policy, downstream: Downstream, now: datetime):
        self.policy = policy
        self.downstream = downstream
        self.now = now
        # (failure class, detail) reported by a classifying (HTTP) downstream during lookups
        self.failures: list[tuple[str, str | None]] = []
        self.account_arg: str | None = None  # ad_account_id exactly as the agent sent it (used for lookups)
        self.not_entity: dict[str, str] = {}  # ids the downstream explicitly said are not account entities
        self.account_id: str | None = None   # the same account, normalised to act_<digits>

    # ---------------------------------------------------------------- downstream reads

    async def _fetch(self, entity_type: str, entity_id: str) -> tuple[str, dict[str, Any] | None]:
        """Returns (found | not_found | unavailable, row)."""
        if entity_type == "creative":
            tool, args = "ads_get_creatives", {"creative_ids": [entity_id], "fields": list(LOOKUP_FIELDS["creative"])}
            if self.account_arg:
                args[ACCOUNT_ARG] = self.account_arg
        else:
            if not self.account_arg:
                return "unavailable", None  # the real lookup requires the account
            tool, args = "ads_get_ad_entities", {ACCOUNT_ARG: self.account_arg, "object_ids": [entity_id],
                                                  "fields": list(LOOKUP_FIELDS[entity_type])}
        try:
            res = await self.downstream.call(tool, args)
        except DownstreamFailure as exc:
            self._record_failure(exc.label, exc.detail)
            return "unavailable", None
        except Exception:
            return "unavailable", None
        data, info = _lookup_rows(res.structured) if res.ok else (None, None)
        if not isinstance(data, list):
            if getattr(self.downstream, "classifies_failures", False):
                # Diagnostic only: the decision is "unavailable" either way.
                if not res.ok:
                    self._record_failure("error", res.detail)
                else:
                    keys = sorted(res.structured) if isinstance(res.structured, dict) else []
                    self._record_failure("malformed", f"{tool} answered without a data list (keys: {keys}); "
                                                      f"{res.detail or ''}".rstrip("; "))
            return "unavailable", None
        id_keys = ("id", f"{entity_type}_id") + (("ad_set_id",) if entity_type == "adset" else ())
        rows = [r for r in data if isinstance(r, dict) and any(str(r.get(k)) == entity_id for k in id_keys)]
        if rows:
            return "found", rows[0]
        if info and entity_id in info and "not" in info.lower():
            # The server said so explicitly ("... are not campaign entities ..."): keep its words.
            from .downstream_http import sanitize_detail
            self.not_entity[entity_id] = sanitize_detail(info) or info[:500]
        return "not_found", None

    async def _locate(self, entity_id: str, entity_type: str | None) -> tuple[str, str | None, dict[str, Any] | None]:
        unavailable = False
        for t in ([entity_type] if entity_type else list(ENTITY_KINDS)):
            status, row = await self._fetch(t, entity_id)
            if status == "found":
                return "found", t, row
            unavailable |= status == "unavailable"
        return ("unavailable" if unavailable else "not_found"), entity_type, None

    def _record_failure(self, label: str, detail: str | None) -> None:
        if all(existing != label for existing, _ in self.failures):
            self.failures.append((label, detail))

    def _take_account(self, args: Any) -> None:
        if isinstance(args, dict) and ACCOUNT_ARG in args:
            self.account_id = _account(args[ACCOUNT_ARG])
            self.account_arg = args[ACCOUNT_ARG]

    def _change(self, entity_type: str | None, entity_id: str | None, field_path: str, before: Any, after: Any,
                change_type: str | None = None, name: str | None = None) -> ChangeRecord:
        if change_type is None:
            change_type = "added" if before is None else ("removed" if after is None else "modified")
        return ChangeRecord(
            id=0, detected_at=self.now, account_id=self.account_id or "", from_snapshot_id=0, to_snapshot_id=0,
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
            self._take_account(args)
            proposal = await getattr(self, f"_interpret_{tool}")(args)
        except Uninterpretable as exc:
            return Assessment(tool, "write", "deny", [_reason("rein:uninterpretable", "deny", f"解釈不能: {exc}")])
        except Unresolvable as exc:
            return Assessment(tool, "write", "deny", [_reason("rein:unresolvable", "deny", f"対象エンティティを特定できない: {exc}")])

        reasons = list(proposal.notes)
        reasons += [_failure_reason(label, detail) for label, detail in self.failures]
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
        args = _check_keys(args, *ASSUMED_ARGUMENTS["ads_update_entity"])
        entity_id = _id(args["entity_id"], "entity_id")
        etype = _entity_type(args["entity_type"])
        fields = args.get("fields")
        if fields is not None and not isinstance(fields, dict):
            raise Uninterpretable("fields がオブジェクトではない")
        status, etype, row = await self._locate(entity_id, etype)
        if status == "not_found":
            raise Unresolvable(f"{entity_id} が下流に存在しない")
        if not fields:
            # Valid per the real schema (fields is optional), but there is no change for Rein to judge.
            proposal = Proposal(etype, entity_id)
            proposal.notes.append(NO_FIELD_CHANGES)
            return proposal
        allowed = UPDATE_FIELDS[etype]
        unknown = set(fields) - allowed
        if unknown:
            raise Uninterpretable(f"{etype} で扱えないフィールド {sorted(unknown)}")
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
        args = _check_keys(args, *ASSUMED_ARGUMENTS["ads_activate_entity"])
        entity_id = _id(args["entity_id"], "entity_id")
        etype = _entity_type(args["entity_type"])
        for flag in ("publish_as_active", "allow_partial_publish"):
            if flag in args and not isinstance(args[flag], bool):
                raise Uninterpretable(f"{flag} は真偽値（true / false）でなければならない: {args[flag]!r}")
        status, etype, row = await self._locate(entity_id, etype)
        if status == "not_found":
            info = self.not_entity.get(entity_id)
            if info is None:
                raise Unresolvable(f"{entity_id} が下流に存在しない")
            # Not an entity by the server's own account (a draft, for instance): queue, with that fact visible.
            proposal = Proposal(etype, entity_id)
            proposal.notes.append(_target_not_entity(entity_id, info))
            proposal.changes.append(self._change(etype, entity_id, "status", None, "ACTIVE"))
        else:
            proposal = await self._update_proposal(etype, entity_id, row, {"status": "ACTIVE"})
        if args.get("publish_as_active") is True:
            # allow_partial_publish only rides along; on its own it changes nothing.
            proposal.notes.append(PUBLISH_NEEDS_APPROVAL)
        return proposal

    # ---------------------------------------------------------------- creates

    def _created(self, entity_type: str, raw: dict[str, Any]) -> Proposal:
        after = normalize_entity(entity_type, {"id": NEW_ENTITY_ID, **raw})
        proposal = Proposal(entity_type, None)
        proposal.changes.append(self._change(entity_type, NEW_ENTITY_ID, "*", None, after, "entity_created",
                                             name=after.get("name")))
        return proposal

    async def _interpret_ads_create_campaign(self, args: Any) -> Proposal:
        args = _check_keys(args, *ASSUMED_ARGUMENTS["ads_create_campaign"])
        raw: dict[str, Any] = {"name": _text(args["campaign_name"], "campaign_name"),
                               "objective": _text(args["objective"], "objective"),
                               "buying_type": _text(args["buying_type"], "buying_type"), "status": "PAUSED"}
        # The real budget arguments carry a campaign_ prefix; budget rules evaluate them as the entity's
        # daily_budget / lifetime_budget / spend_cap.
        for arg, key in (("campaign_daily_budget", "daily_budget"), ("campaign_lifetime_budget", "lifetime_budget"),
                         ("campaign_spend_cap", "spend_cap")):
            if arg in args:
                raw[key] = _budget(args[arg], arg)
        for arg, key in (("campaign_start_time", "start_time"), ("campaign_stop_time", "stop_time")):
            if arg in args:
                raw[key] = _time(args[arg], arg)
        if "special_ad_categories" in args:
            if not isinstance(args["special_ad_categories"], list):
                raise Uninterpretable("special_ad_categories がリストではない")
            raw["special_ad_categories"] = args["special_ad_categories"]
        if "special_ad_category_country" in args:
            if not isinstance(args["special_ad_category_country"], list):
                raise Uninterpretable("special_ad_category_country がリストではない")
            raw["special_ad_category_country"] = args["special_ad_category_country"]
        return self._created("campaign", raw)

    async def _interpret_ads_create_ad_set(self, args: Any) -> Proposal:
        args = _check_keys(args, *ASSUMED_ARGUMENTS["ads_create_ad_set"])
        campaign_id = _id(args["campaign_id"], "campaign_id")
        raw: dict[str, Any] = {"campaign_id": campaign_id, "status": "PAUSED",
                               "name": _text(args["ad_set_name"], "ad_set_name")}
        for key in ("optimization_goal", "billing_event"):
            raw[key] = _text(args[key], key)
        for key in ("daily_budget", "lifetime_budget", "bid_amount"):
            if key in args:
                raw[key] = _budget(args[key], key)
        if not isinstance(args["targeting"], dict):
            raise Uninterpretable("targeting がオブジェクトではない")
        raw["targeting"] = args["targeting"]
        for key in ("start_time", "end_time"):
            if key in args:
                raw[key] = _time(args[key], key)
        status, _ = await self._fetch("campaign", campaign_id)
        if status == "not_found":
            raise Unresolvable(f"親キャンペーン {campaign_id} が存在しない")
        proposal = self._created("adset", raw)
        if status == "unavailable":
            proposal.notes.append(CURRENT_UNAVAILABLE)
        return proposal

    async def _interpret_ads_create_ad(self, args: Any) -> Proposal:
        args = _check_keys(args, *ASSUMED_ARGUMENTS["ads_create_ad"])
        adset_id = _id(args["ad_set_id"], "ad_set_id")
        raw: dict[str, Any] = {"name": _text(args["ad_name"], "ad_name"), "adset_id": adset_id, "status": "PAUSED"}
        adset_status, _ = await self._fetch("adset", adset_id)
        if adset_status == "not_found":
            raise Unresolvable(f"親の広告セット {adset_id} が存在しない")
        notes: list[dict[str, Any]] = []
        creative = args.get("creative")
        cr_status = "found"
        if creative is None:
            notes.append(CREATIVE_NOT_RESOLVED)
        elif isinstance(creative, dict) and set(creative) == {"creative_id"}:
            # The one nested form Rein reads: a reference to an existing creative.
            creative_id = _id(creative["creative_id"], "creative.creative_id")
            raw["creative"] = {"id": creative_id}
            cr_status, cr_row = await self._fetch("creative", creative_id)
            if cr_status == "not_found":
                raise Unresolvable(f"クリエイティブ {creative_id} が存在しない")
            if cr_row is not None:
                # Expose the creative's link/text on the new ad, as snapshots do, so text rules apply.
                raw["creative_resolved"] = normalize_entity("creative", cr_row).get("resolved")
        else:
            # An inline creative spec (or anything else): Rein cannot read its link and text with certainty.
            notes.append(CREATIVE_NOT_RESOLVED)
        proposal = self._created("ad", raw)
        proposal.notes += notes
        if "unavailable" in (adset_status, cr_status):
            proposal.notes.append(CURRENT_UNAVAILABLE)
        return proposal

    async def _interpret_ads_create_creative(self, args: Any) -> Proposal:
        args = _check_keys(args, *ASSUMED_ARGUMENTS["ads_create_creative"])
        return self._created("creative", creative_raw_from_args(args))


def creative_raw_from_args(args: dict[str, Any]) -> dict[str, Any]:
    """Real ads_create_creative arguments -> the Graph API creative shape snapshots and Creative Check read.

    Singular texts go to object_story_spec.link_data; when any text has several variants (primary_texts,
    headlines, descriptions), every variant goes to asset_feed_spec so text rules and claims see all of them.
    """
    link = None
    if "link_url" in args:
        link = _text(args["link_url"], "link_url")
        if not link.startswith(("https://", "http://")):
            raise Uninterpretable(f"link_url が URL として解釈できない: {link!r}")
    bodies = _texts_arg(args, "message", "primary_texts")
    titles = _texts_arg(args, "headline", "headlines")
    descs = _texts_arg(args, "description", "descriptions")
    cta = _text(args["call_to_action_type"], "call_to_action_type") if "call_to_action_type" in args else None
    raw: dict[str, Any] = {"object_story_spec": {}}
    if "name" in args:
        raw["name"] = _text(args["name"], "name")
    if "page_id" in args:
        raw["object_story_spec"]["page_id"] = _id(args["page_id"], "page_id")
    image_hash = _text(args["image_hash"], "image_hash") if "image_hash" in args else None
    if max(len(bodies), len(titles), len(descs)) > 1:
        feed: dict[str, Any] = {}
        if bodies:
            feed["bodies"] = [{"text": t} for t in bodies]
        if titles:
            feed["titles"] = [{"text": t} for t in titles]
        if descs:
            feed["descriptions"] = [{"text": t} for t in descs]
        if link:
            feed["link_urls"] = [{"website_url": link}]
        if cta:
            feed["call_to_action_types"] = [cta]
        if image_hash:
            feed["images"] = [{"hash": image_hash}]
        raw["asset_feed_spec"] = feed
    else:
        link_data: dict[str, Any] = {}
        for key, values in (("message", bodies), ("name", titles), ("description", descs)):
            if values:
                link_data[key] = values[0]
        if link:
            link_data["link"] = link
        if cta:
            link_data["call_to_action"] = {"type": cta}
        if image_hash:
            link_data["image_hash"] = image_hash
        raw["object_story_spec"]["link_data"] = link_data
    return raw


# --------------------------------------------------------------------------- proxy


def _lookup_rows(structured: Any) -> tuple[list[Any] | None, str | None]:
    """Rows and the server's additional note from an entities / creatives lookup answer.

    Accepts the mock's `{"data": [...]}` and the shape the real Ads MCP returned in Phase 5:
    `{"ad_entities": "<JSON list as a string>", "additional_info": "..."}`."""
    if not isinstance(structured, dict):
        return None, None
    info = structured.get("additional_info") if isinstance(structured.get("additional_info"), str) else None
    rows = structured.get("data")
    if rows is None:
        rows = structured.get("ad_entities")
        if isinstance(rows, str):
            try:
                rows = json.loads(rows)
            except ValueError:
                rows = None
    return (rows if isinstance(rows, list) else None), info


def _downstream_detail(reasons: list[dict[str, Any]]) -> dict[str, Any]:
    details = [r["detail"] for r in reasons if r.get("rule_id") == "rein:downstream-failure" and r.get("detail")]
    return {"downstream_detail": details[0]} if details else {}


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
    detail: str | None = None  # what the downstream said when the call was executed (bounded, scrubbed)
    result_kind: str | None = None  # e.g. "created_draft": reporting only, never a state


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
        except DownstreamFailure as exc:  # failure class plus the server's sanitised words (detail)
            return DownstreamResult(False, None, f"downstream error: {exc.label}", detail=exc.detail,
                                    failure=exc.label)
        except Exception as exc:  # a broken downstream is reported, never retried
            return DownstreamResult(False, None, f"downstream error: {type(exc).__name__}",
                                    failure=type(exc).__name__)

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
            if not res.ok and res.detail is not None:
                audit["reasons"] = list(assessment.reasons) + [_failure_reason(
                    res.failure or "error", res.detail, assessment.decision, "下流での実行に失敗")]
            self.ledger.record_audit(**audit, decision=assessment.decision, forwarded=1,
                                     downstream_ok=int(res.ok), result_summary=_short(res.text))
            return res.to_call_result()

        if assessment.decision == "deny":
            audit_id = self.ledger.record_audit(**audit, decision="deny", forwarded=0)
            payload = {"decision": "deny", "executed": False, "reasons": assessment.reasons, "audit_id": audit_id}
            payload.update(_downstream_detail(assessment.reasons))
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
        payload.update(_downstream_detail(assessment.reasons))
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
                "execution_failed": "実行結果がエラーだったため消費済み。再実行しない（新しいコールと承認が必要）",
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
        detail = describe_result(res)
        problem = None if res.failure else error_content(res)
        if res.failure:
            status, reason = "failed", f"下流に届かなかった、または応答を得られなかった: {res.failure}"
        elif problem:
            # Transport success is not execution success: the server's answer says it did not do it.
            status, reason = "execution_failed", f"下流の応答がエラーを示した（{problem}）"
        else:
            status, reason = "executed", None
        ok = status == "executed"
        # Reporting only: what an executed create made. The execution state stays `executed`.
        kind = "created_draft" if ok and approval["tool"].startswith("ads_create_") and is_draft(res) else None
        result = {"ok": ok, "structured": res.structured, "text": _short(res.text, 1000), "detail": detail}
        self.ledger.transition_approval(approval_id, "approved", status, executed_at=iso(self.clock()), result=result,
                                        status_reason=reason, result_kind=kind)
        entity_id = approval["entity_id"] or ((str((res.structured or {}).get("id") or "") or None) if ok else None)
        self._audit_approval(approval, f"approval_{status}", status, operator, approval["findings"], forwarded=1,
                             downstream_ok=int(ok), result_summary=detail, entity_id=entity_id, result_kind=kind)
        message = "承認により 1 回実行した" if ok else f"{reason}。自動では再実行しない（新しいコールと承認が必要）"
        if kind == "created_draft":
            message += "（下流はドラフトとして受け付けた: アカウントのエンティティとは限らない。確認の読み取りをすること）"
        return ApprovalOutcome(approval_id, status, ok, message, result, changed=True, detail=detail, result_kind=kind)


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


async def serve(policy: Policy, db: str, downstream_cm: Any) -> None:
    """Serve the proxy on stdio with the given downstream (an async context manager yielding a Downstream)."""
    async with downstream_cm as downstream:
        proxy = ReinProxy(policy, Ledger(db), downstream)
        server = build_proxy_server(proxy)
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())


async def serve_stdio(policy: Policy, db: str, fixture: str, state: str | None) -> None:
    await serve(policy, db, mock_downstream_over_stdio(fixture, state))
