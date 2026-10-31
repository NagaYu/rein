from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures"
SCENARIOS = FIXTURES / "scenarios"
POLICY_EXAMPLE = ROOT / "configs" / "policy.example.yaml"
SCENARIO_NAMES = sorted(p.name for p in SCENARIOS.iterdir() if p.is_dir())


@pytest.fixture(autouse=True)
def _no_real_env(monkeypatch):
    # Tests must never see a real token or webhook URL from the developer's shell.
    monkeypatch.delenv("REIN_META_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("REIN_ALERT_WEBHOOK_URL", raising=False)
