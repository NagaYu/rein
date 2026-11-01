"""Build fixtures/discovery/assumed_tools_list.json from the real discovery report (discover-live.json).

The live report records, per tool, the property names, the required list and the annotations. It does not
record property types, enum values or nested shapes, so every property in the fixture is an empty schema `{}`.
Only the in-scope tools are copied: Rein's 9 known tools and the 6 reads allowlisted in Phase 5C.
Run: .venv/bin/python scripts/build_discovery_fixture.py
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIVE = ROOT / "discover-live.json"
OUT = ROOT / "fixtures" / "discovery" / "assumed_tools_list.json"
IN_SCOPE = (
    "ads_get_ad_accounts", "ads_get_ad_entities", "ads_get_creatives",
    "ads_update_entity", "ads_activate_entity", "ads_create_campaign", "ads_create_ad_set", "ads_create_ad",
    "ads_create_creative",
    "ads_account_get_activity_logs", "ads_get_ad_images", "ads_get_ad_videos", "ads_get_custom_audience",
    "ads_get_ad_account_pages", "ads_get_ig_accounts",
)


def main() -> None:
    report = json.loads(LIVE.read_text(encoding="utf-8"))
    by_name = {t["name"]: t for t in report["tools"]}
    tools = []
    for name in IN_SCOPE:
        t = by_name[name]
        schema = t["schema"] or {"properties": [], "required": []}
        tools.append({"name": name,
                      "inputSchema": {"type": "object", "properties": {p: {} for p in schema["properties"]},
                                      "required": schema["required"]},
                      "annotations": t["annotations"]})
    doc = {
        "_note": ("REAL NAMES, UNKNOWN TYPES. Property names, required lists and annotations are copied from "
                  "discover-live.json (rein proxy discover against the real Ads MCP, server "
                  f"{report['server']['name']} {report['server']['version']}) for the {len(tools)} tools in scope. "
                  "The report does not record property types, enum values or nested shapes, so each property is an "
                  "empty schema. Built by scripts/build_discovery_fixture.py."),
        "serverInfo": {"name": "real-shapes (fixture)", "version": report["server"]["version"]},
        "tools": tools,
    }
    OUT.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)} ({len(tools)} tools)")


if __name__ == "__main__":
    main()
