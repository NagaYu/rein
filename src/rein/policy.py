"""Protection rules (policy.yaml) and their evaluation.

The same rules serve two scopes:
- watch (Phase 1): evaluate changes found by diffing snapshots.
  budget_cap / frozen_fields / prohibited_terms -> "violation"; the others -> "warning".
- proxy (Phase 2): evaluate the change a tool call *would* make, before it runs.
  Each rule maps to a decision via `on_proxy` (deny | require_approval); see PROXY_DEFAULTS.
"""

from __future__ import annotations

import fnmatch
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, time
from fractions import Fraction
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from .attribution import ACTOR_KINDS, AttributionConfig
from .client import ENTITY_TYPES
from .diff import flatten
from .ledger import ChangeRecord
from .timeutil import parse_time

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
SEVERITY_ORDER = {None: 0, "warning": 1, "violation": 2}
FIELD_CHANGE_TYPES = ("modified", "added", "removed")
CHANGE_TYPES = ("modified", "added", "removed", "entity_created", "entity_deleted")
SCOPES = ("watch", "proxy")
PROXY_DECISIONS = ("deny", "require_approval")
# Fixed Phase 2 semantics: hard limits deny, judgement calls go to a human.
PROXY_DEFAULTS = {
    "budget_cap": "deny",
    "frozen_fields": "deny",
    "prohibited_terms": "deny",
    "change_window": "require_approval",
    "budget_increase_ratio": "require_approval",
    "approval_required": "require_approval",
}
# Rule types added in Phase 2 apply to the proxy only unless `scopes` says otherwise,
# so Phase 1 watch results do not change.
DEFAULT_SCOPES = {
    "budget_cap": SCOPES, "frozen_fields": SCOPES, "prohibited_terms": SCOPES, "change_window": SCOPES,
    "budget_increase_ratio": ("proxy",), "approval_required": ("proxy",),
}


class PolicyError(ValueError):
    pass


@dataclass
class Finding:
    rule_id: str
    rule_type: str
    severity: str
    reason: str
    details: dict[str, Any] = field(default_factory=dict)
    on_proxy: str | None = None  # set only when evaluated in the proxy scope

    def to_dict(self) -> dict[str, Any]:
        d = {"rule_id": self.rule_id, "rule_type": self.rule_type, "severity": self.severity,
             "reason": self.reason, "details": self.details}
        if self.on_proxy:
            d["on_proxy"] = self.on_proxy
        return d


@dataclass
class Evaluation:
    findings: list[Finding] = field(default_factory=list)

    @property
    def severity(self) -> str | None:
        return max((f.severity for f in self.findings), key=SEVERITY_ORDER.__getitem__, default=None)

    @property
    def violation(self) -> bool:
        return self.severity == "violation"


# --------------------------------------------------------------------------- rules


@dataclass
class Rule:
    id: str
    entity_types: tuple[str, ...]
    entity_ids: frozenset[str]
    description: str | None
    scopes: frozenset[str]
    on_proxy: str

    type = ""
    severity = "violation"

    def applies_to(self, change: ChangeRecord) -> bool:
        if self.entity_types and change.entity_type not in self.entity_types:
            return False
        return not self.entity_ids or change.entity_id in self.entity_ids

    def finding(self, reason: str, **details: Any) -> Finding:
        return Finding(self.id, self.type, self.severity, reason, details)

    def evaluate(self, change: ChangeRecord, policy: "Policy") -> list[Finding]:
        raise NotImplementedError


@dataclass
class BudgetCapRule(Rule):
    fields: tuple[str, ...] = ()
    max: int = 0
    type = "budget_cap"

    def evaluate(self, change, policy):
        if not self.applies_to(change):
            return []
        if change.change_type == "entity_created":
            candidates = [(f, (change.after or {}).get(f)) for f in self.fields]
        elif change.change_type in ("modified", "added") and change.field in self.fields:
            candidates = [(change.field, change.after)]
        else:
            return []
        out = []
        for fname, value in candidates:
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int):
                out.append(self.finding(f"{fname} が数値でないため上限 {self.max} を検証できない: {value!r}",
                                        field=fname, value=value, max=self.max))
            elif value > self.max:
                out.append(self.finding(f"{fname} {change.before} → {value} が上限 {self.max} を超過",
                                        field=fname, value=value, max=self.max))
        return out


@dataclass
class FrozenFieldsRule(Rule):
    fields: tuple[str, ...] = ()
    include_deletion: bool = False
    type = "frozen_fields"

    def evaluate(self, change, policy):
        if not self.applies_to(change):
            return []
        if change.change_type == "entity_deleted" and self.include_deletion:
            return [self.finding("変更禁止の対象エンティティがスナップショットから消えた（削除・アーカイブ・権限変更のいずれか）")]
        if change.change_type not in FIELD_CHANGE_TYPES:
            return []
        pattern = next((p for p in self.fields if fnmatch.fnmatchcase(change.field, p)), None)
        if pattern is None:
            return []
        return [self.finding(f"変更禁止フィールド {change.field} が変更された: {change.before!r} → {change.after!r}",
                             field=change.field, pattern=pattern)]


def _fold(text: str, case_sensitive: bool) -> str:
    text = unicodedata.normalize("NFKC", text)
    return text if case_sensitive else text.casefold()


def _texts(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str)]
    return []


@dataclass
class ProhibitedTermsRule(Rule):
    terms: tuple[str, ...] = ()
    fields: tuple[str, ...] = ()
    case_sensitive: bool = False
    type = "prohibited_terms"

    def _matches(self, path: str) -> bool:
        return any(fnmatch.fnmatchcase(path, p) for p in self.fields)

    def evaluate(self, change, policy):
        if not self.applies_to(change):
            return []
        if change.change_type == "entity_created":
            targets = [(p, v) for p, v in flatten(change.after or {}).items() if self._matches(p)]
            before_texts: dict[str, list[str]] = {}
        elif change.change_type in ("modified", "added") and self._matches(change.field):
            targets = [(change.field, change.after)]
            before_texts = {change.field: _texts(change.before)}
        else:
            return []
        out = []
        for path, value in targets:
            for text in _texts(value):
                folded = _fold(text, self.case_sensitive)
                for term in self.terms:
                    if _fold(term, self.case_sensitive) in folded:
                        pre = any(_fold(term, self.case_sensitive) in _fold(t, self.case_sensitive)
                                  for t in before_texts.get(path, []))
                        note = "（変更前から含まれていた）" if pre else ""
                        out.append(self.finding(f"{path} に禁止語句「{term}」{note}", field=path, term=term, text=text,
                                                preexisting=pre))
        return out


@dataclass
class BudgetIncreaseRatioRule(Rule):
    """A single change may raise a budget by at most `max_increase_pct` percent of its current value."""

    fields: tuple[str, ...] = ()
    max_increase_pct: Fraction = Fraction(0)
    type = "budget_increase_ratio"
    severity = "warning"

    def evaluate(self, change, policy):
        if not self.applies_to(change) or change.field not in self.fields:
            return []
        if change.change_type not in ("modified", "added"):
            return []
        before, after = change.before, change.after
        if not isinstance(after, int) or isinstance(after, bool):
            return [self.finding(f"{change.field} の変更後の値が数値でないため増加率を判定できない: {after!r}",
                                 field=change.field)]
        if not isinstance(before, int) or isinstance(before, bool) or before <= 0:
            return [self.finding(f"{change.field} の現在値が無い（{before!r}）ため増加率を判定できない",
                                 field=change.field, before=before, after=after)]
        if after <= before:
            return []
        if Fraction(after - before) * 100 > self.max_increase_pct * before:
            pct = Fraction(after - before) * 100 / before
            return [self.finding(f"{change.field} {before} → {after}（+{float(pct):.1f}%）が 1 回あたりの増額上限 "
                                 f"+{float(self.max_increase_pct):g}% を超える",
                                 field=change.field, before=before, after=after, increase_pct=float(pct))]
        return []


@dataclass
class ApprovalRequiredRule(Rule):
    """Matches by change type (and optionally field pattern); the proxy sends matches to a human."""

    change_types: tuple[str, ...] = ()
    fields: tuple[str, ...] = ()
    type = "approval_required"
    severity = "warning"

    def evaluate(self, change, policy):
        if not self.applies_to(change) or change.change_type not in self.change_types:
            return []
        if self.fields and not any(fnmatch.fnmatchcase(change.field, p) for p in self.fields):
            return []
        what = "新規作成" if change.change_type == "entity_created" else f"{change.field} の変更"
        return [self.finding(f"{change.entity_type} の{what}は人の承認が必要", change_type=change.change_type)]


@dataclass
class Window:
    days: frozenset[str]
    start: time
    end: time

    def contains(self, local: datetime) -> bool:
        day = DAYS[local.weekday()]
        t = local.time().replace(tzinfo=None)
        if self.start <= self.end:
            return day in self.days and self.start <= t < self.end
        # Overnight window, e.g. 22:00-06:00: the early-morning part belongs to the previous day.
        if t >= self.start:
            return day in self.days
        if t < self.end:
            return DAYS[(local.weekday() - 1) % 7] in self.days
        return False

    def describe(self) -> str:
        days = ",".join(d for d in DAYS if d in self.days)
        return f"{days} {self.start:%H:%M}-{self.end:%H:%M}"


def change_time(change: ChangeRecord) -> tuple[datetime, str]:
    """Best available time the change happened, with the basis used."""
    requested = (change.attribution or {}).get("requested_at")
    if requested:
        return parse_time(requested), "proxy.requested_at（プロキシがコールを受け付けた時刻）"
    acts = (change.attribution or {}).get("activities") or []
    times = []
    for a in acts:
        try:
            times.append(parse_time(a["event_time"]))
        except (KeyError, ValueError, TypeError):
            pass
    if times:
        return max(times), "activity.event_time"
    if change.entity_updated_time:
        window = (change.attribution or {}).get("window") or {}
        t = parse_time(change.entity_updated_time)
        start = parse_time(window["start"]) if window.get("start") else None
        if (start is None or t > start) and t <= change.detected_at:
            return t, "entity.updated_time"
    return change.detected_at, "detected_at（実際の変更時刻は不明。スナップショット取得時刻で代用）"


@dataclass
class ChangeWindowRule(Rule):
    windows: tuple[Window, ...] = ()
    timezone: str = "UTC"
    type = "change_window"
    severity = "warning"

    def evaluate(self, change, policy):
        if not self.applies_to(change):
            return []
        when, basis = change_time(change)
        local = when.astimezone(ZoneInfo(self.timezone))
        if any(w.contains(local) for w in self.windows):
            return []
        allowed = " / ".join(w.describe() for w in self.windows)
        return [self.finding(f"想定時間帯外の変更: {local:%Y-%m-%d %a %H:%M} {self.timezone}（許可: {allowed}、根拠: {basis}）",
                             local_time=local.isoformat(), basis=basis)]


# --------------------------------------------------------------------------- loading


@dataclass
class AlertConfig:
    file: str | None = "alerts.jsonl"
    webhook_url_env: str = "REIN_ALERT_WEBHOOK_URL"


@dataclass
class ProxyConfig:
    read_tools: frozenset[str] = frozenset()  # tools passed through without evaluation
    approval_ttl_hours: Fraction = Fraction(24)


@dataclass
class Policy:
    rules: list[Rule]
    timezone: str = "UTC"
    attribution: AttributionConfig = field(default_factory=AttributionConfig)
    alerts: AlertConfig = field(default_factory=AlertConfig)
    proxy: ProxyConfig = field(default_factory=ProxyConfig)
    version: int = 1


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise PolicyError(msg)


def _str_list(value: Any, name: str, rid: str) -> tuple[str, ...]:
    if isinstance(value, str):
        value = [value]
    _require(isinstance(value, list) and value and all(isinstance(v, str) and v for v in value),
             f"rule {rid}: {name} must be a non-empty list of non-empty strings")
    return tuple(value)


def _parse_time_of_day(value: Any, rid: str) -> time:
    try:
        hh, mm = str(value).split(":")
        return time(int(hh), int(mm))
    except ValueError as exc:
        raise PolicyError(f"rule {rid}: invalid time {value!r} (use HH:MM)") from exc


def _zone(name: str, where: str) -> str:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise PolicyError(f"{where}: unknown timezone {name!r}") from exc
    return name


_ALLOWED_KEYS = {
    "budget_cap": {"fields", "field", "max"},
    "frozen_fields": {"fields", "include_deletion"},
    "prohibited_terms": {"terms", "fields", "case_sensitive"},
    "change_window": {"windows", "days", "start", "end", "timezone"},
    "budget_increase_ratio": {"fields", "field", "max_increase_pct"},
    "approval_required": {"change_types", "fields"},
}
_COMMON_KEYS = {"id", "type", "entity_types", "entity_ids", "description", "scopes", "on_proxy"}


def _number(value: Any, what: str) -> Fraction:
    _require(isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0,
             f"{what} must be a non-negative number")
    return Fraction(str(value)) if isinstance(value, float) else Fraction(value)


def _parse_rule(raw: dict[str, Any], default_tz: str) -> Rule:
    _require(isinstance(raw, dict), "each rule must be a mapping")
    rid, rtype = raw.get("id"), raw.get("type")
    _require(isinstance(rid, str) and rid, "rule without id")
    _require(rtype in _ALLOWED_KEYS, f"rule {rid}: unknown type {rtype!r}")
    unknown = set(raw) - _COMMON_KEYS - _ALLOWED_KEYS[rtype]
    _require(not unknown, f"rule {rid}: unknown keys {sorted(unknown)}")
    entity_types = _str_list(raw.get("entity_types", []), "entity_types", rid) if raw.get("entity_types") else ()
    _require(all(t in ENTITY_TYPES for t in entity_types), f"rule {rid}: entity_types must be among {ENTITY_TYPES}")
    scopes = _str_list(raw["scopes"], "scopes", rid) if "scopes" in raw else DEFAULT_SCOPES[rtype]
    _require(set(scopes) <= set(SCOPES), f"rule {rid}: scopes must be among {SCOPES}")
    on_proxy = raw.get("on_proxy", PROXY_DEFAULTS[rtype])
    _require(on_proxy in PROXY_DECISIONS, f"rule {rid}: on_proxy must be one of {PROXY_DECISIONS}")
    common = dict(id=rid, entity_types=entity_types,
                  entity_ids=frozenset(str(i) for i in raw.get("entity_ids") or []),
                  description=raw.get("description"), scopes=frozenset(scopes), on_proxy=on_proxy)

    if rtype == "budget_cap":
        fields = _str_list(raw.get("fields", raw.get("field")), "fields", rid)
        _require(set(fields) <= {"daily_budget", "lifetime_budget", "spend_cap"},
                 f"rule {rid}: budget_cap fields must be daily_budget / lifetime_budget / spend_cap")
        _require(set(entity_types or ()) <= {"campaign", "adset"} and entity_types != (),
                 f"rule {rid}: budget_cap entity_types must be campaign and/or adset")
        cap = raw.get("max")
        _require(isinstance(cap, int) and not isinstance(cap, bool) and cap >= 0,
                 f"rule {rid}: max must be a non-negative integer (API units of the account currency)")
        return BudgetCapRule(**common, fields=fields, max=cap)
    if rtype == "frozen_fields":
        return FrozenFieldsRule(**common, fields=_str_list(raw.get("fields"), "fields", rid),
                                include_deletion=bool(raw.get("include_deletion", False)))
    if rtype == "prohibited_terms":
        return ProhibitedTermsRule(**common, terms=_str_list(raw.get("terms"), "terms", rid),
                                   fields=_str_list(raw.get("fields"), "fields", rid),
                                   case_sensitive=bool(raw.get("case_sensitive", False)))
    if rtype == "budget_increase_ratio":
        fields = _str_list(raw.get("fields", raw.get("field")), "fields", rid)
        _require(set(fields) <= {"daily_budget", "lifetime_budget", "spend_cap"},
                 f"rule {rid}: budget_increase_ratio fields must be daily_budget / lifetime_budget / spend_cap")
        _require(set(entity_types or ()) <= {"campaign", "adset"} and entity_types != (),
                 f"rule {rid}: budget_increase_ratio entity_types must be campaign and/or adset")
        return BudgetIncreaseRatioRule(**common, fields=fields,
                                       max_increase_pct=_number(raw.get("max_increase_pct"), f"rule {rid}: max_increase_pct"))
    if rtype == "approval_required":
        change_types = _str_list(raw.get("change_types"), "change_types", rid)
        _require(set(change_types) <= set(CHANGE_TYPES), f"rule {rid}: change_types must be among {CHANGE_TYPES}")
        fields = _str_list(raw["fields"], "fields", rid) if "fields" in raw else ()
        return ApprovalRequiredRule(**common, change_types=change_types, fields=fields)
    # change_window
    specs = raw.get("windows") or [{k: raw[k] for k in ("days", "start", "end") if k in raw}]
    windows = []
    for spec in specs:
        days = tuple(d.lower() for d in _str_list(spec.get("days", list(DAYS)), "days", rid))
        _require(all(d in DAYS for d in days), f"rule {rid}: days must be among {DAYS}")
        _require("start" in spec and "end" in spec, f"rule {rid}: window needs start and end")
        windows.append(Window(frozenset(days), _parse_time_of_day(spec["start"], rid), _parse_time_of_day(spec["end"], rid)))
    tz = _zone(raw.get("timezone", default_tz), f"rule {rid}")
    return ChangeWindowRule(**common, windows=tuple(windows), timezone=tz)


def _parse_attribution(raw: dict[str, Any] | None) -> AttributionConfig:
    raw = raw or {}
    out = {}
    for section in ("applications", "actors"):
        entries = {}
        for key, val in (raw.get(section) or {}).items():
            val = {"kind": val} if isinstance(val, str) else dict(val)
            _require(val.get("kind") in ACTOR_KINDS, f"attribution.{section}.{key}: kind must be one of {ACTOR_KINDS}")
            entries[str(key)] = {"kind": val["kind"], "label": str(val.get("label", ""))}
        out[section] = entries
    return AttributionConfig(**out)


def parse_policy(raw: dict[str, Any]) -> Policy:
    _require(isinstance(raw, dict), "policy must be a mapping")
    _require(raw.get("version", 1) == 1, "unsupported policy version")
    tz = _zone(raw.get("timezone", "UTC"), "policy")
    rules = [_parse_rule(r, tz) for r in raw.get("rules") or []]
    ids = [r.id for r in rules]
    _require(len(ids) == len(set(ids)), "duplicate rule ids")
    alerts_raw = raw.get("alerts") or {}
    alerts = AlertConfig(file=alerts_raw.get("file", "alerts.jsonl"),
                         webhook_url_env=alerts_raw.get("webhook_url_env", "REIN_ALERT_WEBHOOK_URL"))
    proxy_raw = raw.get("proxy") or {}
    _require(isinstance(proxy_raw, dict), "proxy must be a mapping")
    unknown = set(proxy_raw) - {"read_tools", "approval_ttl_hours"}
    _require(not unknown, f"proxy: unknown keys {sorted(unknown)}")
    read_tools = _str_list(proxy_raw["read_tools"], "read_tools", "proxy") if proxy_raw.get("read_tools") else ()
    ttl = _number(proxy_raw.get("approval_ttl_hours", 24), "proxy.approval_ttl_hours")
    _require(ttl > 0, "proxy.approval_ttl_hours must be greater than 0")
    return Policy(rules=rules, timezone=tz, attribution=_parse_attribution(raw.get("attribution")), alerts=alerts,
                  proxy=ProxyConfig(read_tools=frozenset(read_tools), approval_ttl_hours=ttl))


def load_policy(path: str | Path) -> Policy:
    with open(path, encoding="utf-8") as fh:
        return parse_policy(yaml.safe_load(fh) or {})


def evaluate(change: ChangeRecord, policy: Policy, scope: str = "watch") -> Evaluation:
    findings: list[Finding] = []
    for rule in policy.rules:
        if scope not in rule.scopes:
            continue
        for f in rule.evaluate(change, policy):
            if scope == "proxy":
                f.on_proxy = rule.on_proxy
            findings.append(f)
    return Evaluation(findings)
