"""Alerts for flagged changes: JSON Lines file, plus a webhook stub that only POSTs when a URL is
configured in the environment."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urlparse

import httpx

from .timeutil import iso

if TYPE_CHECKING:
    from .ledger import ChangeRecord
    from .revert import RevertPlan


def _short(value: Any, limit: int = 80) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def summarize(change: "ChangeRecord") -> str:
    label = f"{change.entity_type} {change.entity_id}" + (f"（{change.entity_name}）" if change.entity_name else "")
    if change.change_type == "entity_created":
        return f"{label} が新規作成された"
    if change.change_type == "entity_deleted":
        return f"{label} がスナップショットから消えた"
    return f"{label} {change.field}: {_short(change.before)} → {_short(change.after)}"


def build_alert(change: "ChangeRecord", plan: "RevertPlan | None") -> dict[str, Any]:
    revert: dict[str, Any] = {"command": f"rein revert --change {change.id}", "dry_run_by_default": True}
    if plan is not None:
        revert["steps"] = [
            {"order": s.order, "action": s.action, "entity": f"{s.entity_type} {s.entity_id}",
             "params": s.params, "note": s.note or None}
            for s in plan.steps
        ]
    else:
        revert["note"] = "警告（要注意）のため計画は自動添付していない。必要ならコマンドで計画を表示できる。"
    return {
        "kind": "rein.alert",
        "version": 1,
        "severity": change.severity,
        "change_id": change.id,
        "account_id": change.account_id,
        "detected_at": iso(change.detected_at),
        "summary": summarize(change),
        "actor_kind": change.actor_kind,
        "attribution_basis": (change.attribution or {}).get("basis"),
        "rules": [f["rule_id"] for f in change.findings],
        "reasons": [f["reason"] for f in change.findings],
        "revert": revert,
    }


class AlertSink(Protocol):
    name: str

    def send(self, alert: dict[str, Any]) -> dict[str, Any]: ...


class FileAlertSink:
    name = "file"

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def send(self, alert: dict[str, Any]) -> dict[str, Any]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(alert, ensure_ascii=False, sort_keys=True) + "\n")
        return {"sink": self.name, "status": "written", "path": str(self.path)}


class WebhookAlertSink:
    """POSTs the alert as JSON only if the URL env var is set. Phase 1 ships this as an interface;
    tests use httpx.MockTransport. The URL itself is never recorded (it may embed a secret)."""

    name = "webhook"

    def __init__(self, url_env: str = "REIN_ALERT_WEBHOOK_URL", *, transport: httpx.BaseTransport | None = None,
                 timeout: float = 5.0):
        self.url_env = url_env
        self._transport = transport
        self._timeout = timeout

    def send(self, alert: dict[str, Any]) -> dict[str, Any]:
        url = os.environ.get(self.url_env)
        if not url:
            return {"sink": self.name, "status": "skipped", "reason": f"{self.url_env} not set"}
        parsed = urlparse(url)
        if parsed.scheme != "https":
            return {"sink": self.name, "status": "skipped", "reason": "webhook URL must be https"}
        try:
            with httpx.Client(transport=self._transport, timeout=self._timeout) as http:
                resp = http.post(url, json=alert)
        except httpx.HTTPError as exc:
            return {"sink": self.name, "status": "failed", "host": parsed.hostname, "error": type(exc).__name__}
        status = "sent" if resp.is_success else "failed"
        return {"sink": self.name, "status": status, "host": parsed.hostname, "http_status": resp.status_code}


def dispatch(alert: dict[str, Any], sinks: list[AlertSink]) -> list[dict[str, Any]]:
    results = []
    for sink in sinks:
        try:
            results.append(sink.send(alert))
        except Exception as exc:  # one broken sink must not hide the alert from the others
            results.append({"sink": getattr(sink, "name", "?"), "status": "failed", "error": type(exc).__name__})
    return results
