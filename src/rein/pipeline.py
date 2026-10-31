"""Orchestration shared by the CLI and the demo."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .alerts import AlertSink, build_alert, dispatch
from .attribution import AttributionConfig, attribute
from .client import AdsReader
from .diff import diff_snapshots
from .ledger import ChangeRecord, Ledger
from .policy import Evaluation, Policy, evaluate
from .revert import plan_for_changes
from .snapshot import take_snapshot

if TYPE_CHECKING:
    from .creative import CreativeWatch


@dataclass
class IngestResult:
    snapshot_id: int
    previous_snapshot_id: int | None
    change_ids: list[int] = field(default_factory=list)


def ingest(ledger: Ledger, reader: AdsReader, attribution: AttributionConfig | None = None) -> IngestResult:
    """Take a snapshot, store it, and record field-level changes against the previous one."""
    snap = take_snapshot(reader)
    sid = ledger.save_snapshot(snap)
    prev_id = ledger.previous_snapshot_id(snap.account_id, sid)
    if prev_id is None:
        return IngestResult(sid, None)
    prev = ledger.load_snapshot(prev_id)
    items = [
        (change, attribute(change, snap.activities, prev.taken_at, snap.taken_at, attribution))
        for change in diff_snapshots(prev, snap)
    ]
    ids = ledger.record_changes(snap.account_id, prev_id, sid, snap.taken_at, items)
    return IngestResult(sid, prev_id, ids)


@dataclass
class CheckResult:
    change: ChangeRecord
    evaluation: Evaluation
    alert: dict[str, Any] | None = None
    deliveries: list[dict[str, Any]] = field(default_factory=list)


def check(ledger: Ledger, policy: Policy, sinks: list[AlertSink], *, include_evaluated: bool = False) -> list[CheckResult]:
    """Evaluate changes against the policy, store findings, and alert on flagged ones."""
    results = []
    for change in ledger.changes_to_evaluate(include_evaluated=include_evaluated):
        ev = evaluate(change, policy)
        ledger.set_evaluation(change.id, ev.severity, [f.to_dict() for f in ev.findings])
        change = ledger.get_change(change.id)
        result = CheckResult(change, ev)
        if ev.severity is not None:
            plan = plan_for_changes([change], ledger) if ev.severity == "violation" else None
            alert = build_alert(change, plan)
            result.alert = alert
            result.deliveries = dispatch(alert, sinks)
            ledger.record_alert(change.id, alert, result.deliveries)
        results.append(result)
    return results


@dataclass
class WatchResult:
    changes: list[CheckResult]
    creatives: list[dict[str, Any]] = field(default_factory=list)


def watch_check(ledger: Ledger, policy: Policy, sinks: list[AlertSink], *, include_evaluated: bool = False,
                creative: "CreativeWatch | None" = None) -> WatchResult:
    """`check`, then (Phase 3) Creative Check on every creative the evaluated changes brought in.
    High-band creative results go down the same alert path as policy findings."""
    results = check(ledger, policy, sinks, include_evaluated=include_evaluated)
    if creative is None:
        return WatchResult(results)
    from .creative import watch_changes
    return WatchResult(results, watch_changes(ledger, [r.change for r in results], creative, sinks))
