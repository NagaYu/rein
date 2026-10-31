from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from rein.ledger import ChangeRecord

DETECTED = datetime(2026, 10, 28, 0, 0, tzinfo=timezone.utc)  # Wed 09:00 JST


def change(entity_type="adset", field="daily_budget", before=None, after=None, change_type="modified",
           entity_id="201", attribution: dict[str, Any] | None = None, updated_time=None,
           detected_at=DETECTED, actor_kind="unknown") -> ChangeRecord:
    return ChangeRecord(
        id=1, detected_at=detected_at, account_id="act_1", from_snapshot_id=1, to_snapshot_id=2,
        entity_type=entity_type, entity_id=entity_id, entity_name=None, field=field, change_type=change_type,
        before=before, after=after, entity_updated_time=updated_time, actor_kind=actor_kind,
        attribution=attribution or {"basis": "no_matching_activity",
                                    "window": {"start": "2026-10-27T00:00:00Z", "end": "2026-10-28T00:00:00Z"}},
        evaluated_at=None, violation=False, severity=None, findings=[],
    )


def run_scenario(name: str, sinks=None, policy=None):
    """Ingest before/after of a scenario into an in-memory ledger and run check."""
    from conftest import POLICY_EXAMPLE, SCENARIOS
    from rein.client import MockAdsClient
    from rein.ledger import Ledger
    from rein.pipeline import check, ingest
    from rein.policy import load_policy

    policy = policy or load_policy(POLICY_EXAMPLE)
    ledger = Ledger(":memory:")
    ingest(ledger, MockAdsClient(SCENARIOS / name / "before"), policy.attribution)
    ingest(ledger, MockAdsClient(SCENARIOS / name / "after"), policy.attribution)
    results = check(ledger, policy, sinks or [])
    return ledger, results
