# Rein

**A pre-execution control layer for AI agents that work inside Meta ad accounts.** Rein checks an agent's
Ads MCP tool calls before they run (allow / require approval / deny), keeps a field-level ledger of what changed in
the account and who changed it, plans reverts, checks AI-written creatives against approved originals, and exports a
hashed evidence pack.

> **Status: built and tested against fixtures and a mock Ads MCP server only. Not connected to a real ad account.**
>
> - Every account, ID, business, person and app in this repository is fictional. The proxy's downstream is a mock
>   Ads MCP server that runs in memory; nothing here talks to Meta's Ads MCP or the Marketing API.
> - Every number on this page is a scenario result against those fixtures (pass / fail of written-down
>   expectations). None of them is a measurement from real ad operations.
> - Rein is a tool for evidence and control. It does not guarantee ad performance, compliance with platform
>   policies or law, or who is legally responsible for a change. Creative Check scores rank what a person should
>   review first; they are not a compliance judgement. Claim extraction only finds declared patterns, so reworded
>   claims get through. Evidence-pack hashes detect changes made after export, nothing earlier.
>
> The full list is under [Limitations](#limitations).

## What it does

Rein has three layers. They share one SQLite ledger.

| Layer | What it does | What it can control |
|---|---|---|
| **MCP proxy** | Sits between an agent and the Ads MCP server. Each write call is evaluated against `policy.yaml` **before** it runs: `allow` forwards it, `require_approval` queues it for a person, `deny` blocks it. Every call and every approval event goes to an audit log. Fail-closed: unknown tools, uninterpretable arguments and unresolvable targets are denied; a missing current value means approval, never allow. | Only calls that go **through the proxy**. |
| **Watch + Revert** | Takes snapshots of campaigns, ad sets, ads and creatives, diffs them field by field, and writes one ledger row per change with an actor (`human` / `external_agent` / `meta_ai` / `unknown`) and the evidence for it. Policy rules flag violations and warnings, alerts are written, and a revert plan restores the previous values. Reverts execute against a mock client only. | Changes made **any** way, including by Meta's own AI features, but only after the fact: detection and revert, not prevention. |
| **Creative Check + evidence pack** | Matches a new or rewritten creative to its approved original, diffs it (a landing-link change is always the top finding), extracts claims with declared patterns, and applies prohibited-term, needs-substantiation and brand rules. The result is a 0–100 review-priority score with bands low / medium / high. The evidence pack merges the Watch ledger with the proxy audit and approvals into one timeline and writes a sha256 manifest that `rein evidence verify` checks. | What the declared rules and patterns cover. |

**Where the line is.** The proxy only sees agents that are configured to call Ads MCP through it. Changes that Meta's
own AI features make inside the platform (for example Advantage+ creative rewrites) never pass through a proxy. Those
are Watch + Revert territory: Rein detects them in the next snapshot and plans a revert. It cannot stop them in advance.

## Quickstart

Needs Python 3.12 and no network after installing dependencies. No Meta account or token is needed; everything runs
on fixtures and mocks.

```bash
git clone https://github.com/NagaYu/rein.git
cd rein
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt -e .
```

Run all three layers end to end on one demo ledger (main path):

```bash
.venv/bin/python scripts/demo_all.py
```

Run the test suite:

```bash
.venv/bin/python -m pytest -q
```

The per-layer demos are still there:

```bash
.venv/bin/python scripts/demo.py           # Watch + Revert: 5 scenarios
.venv/bin/python scripts/demo_proxy.py     # MCP proxy: 14 scenarios
.venv/bin/python scripts/demo_evidence.py  # Creative Check + evidence pack
```

`demo_all.py` builds one SQLite ledger on a simulated clock (2026-10-27 to 2026-10-31) and prints, in order:

1. baseline snapshots of two fictional accounts and the registered creative originals;
2. ten agent tool calls through the proxy with each decision and rule, then the approval queue (one executed, one
   rejected, one expired, plus a refused double approval);
3. the next Watch snapshot: ten changes with actor and severity, the revert plan executed on the mock, and the two
   Watch changes that line up with proxy-forwarded calls;
4. Creative Check results for every new or changed creative;
5. an evidence pack export, a passing verify, and a failing verify on a copy with one edited line.

Language note: the CLI output, rule messages and fixture ad copy are in Japanese, because the fictional ads being
checked are Japanese. The code, configuration keys and this documentation are in English.

## Results

All results below come from the fixtures in this repository and the test suite that checks them. They show that the
rules behave as written on fictional data. They are not field measurements.

### Watch + Revert: 5 scenarios ([fixtures/scenarios/](fixtures/scenarios/))

| # | Scenario | Expected and observed |
|---|---|---|
| 1 | Ad set daily budget 5000 → 50000 | `budget_cap` violation; actor `external_agent` (declared app in the activity history) |
| 2 | Ad's creative swapped so the landing link moves to another domain | `frozen_fields` violation; no matching activity, so actor `unknown` |
| 3 | Swapped copy contains full-width "ｎｏ．１" and "絶対" ("absolutely") | `prohibited_terms` violation after NFKC normalization; actor `meta_ai` (declared app) |
| 4 | Ad set paused on Wednesday 03:12 JST | `change_window` warning; actor `human` |
| 5 | Weekday 13:00 budget 5000 → 8000 | No finding |

### MCP proxy: 14 scenarios ([fixtures/proxy/scenarios.json](fixtures/proxy/scenarios.json))

| # | Call | Expected and observed decision | Downstream changed |
|---|---|---|---|
| 1 | Ad set 2 daily budget 4000 → 4400 (+10%) | allow | yes |
| 2 | Ad set 1 daily budget 5000 → 50000 | deny (over the cap) | no |
| 3 | Ad set 1 daily budget 5000 → 9000 (+80%, under the cap) | require_approval (step-up over +50%) | no |
| 4 | Swap ad 2's creative to one with a different landing link | deny | no |
| 5 | Create a new campaign | require_approval | no |
| 6 | Pause ad set 2 on Thursday 03:00 JST | require_approval (outside business hours) | no |
| 7 | Create a creative with "【ｎｏ．１】…絶対やせる…" ("No.1 … absolutely slims you") | deny (prohibited terms) | no |
| 8 | `ads_get_ad_entities` (read) | passthrough, audited | no |
| 9 | `ads_delete_entity` (not on the allow list) | deny (unknown tool) | no |
| 10 | `daily_budget: "lots"` | deny (uninterpretable) | no |
| 11 | Approve call 3 | executed exactly once | yes (once) |
| 12 | Reject call 3, then approve | stays rejected | no |
| 13 | Approve call 3 after 24 hours | expired | no |
| 14 | Approve call 3 twice | executed, second approval refused | yes (once) |

### Creative Check and evidence pack: 12 scenarios ([fixtures/creative/cases.json](fixtures/creative/cases.json), [tests/test_evidence.py](tests/test_evidence.py))

Weights: critical 50, major 20, minor 5, info 0. Bands: medium from 25, high from 50, capped at 100. All of these are
declared in [configs/creative_rules.example.yaml](configs/creative_rules.example.yaml).

| # | Scenario | Expected and observed |
|---|---|---|
| 1 | Candidate identical to the original | No detections, score 0, low |
| 2 | Body contains the prohibited term "絶対" | critical detection, 55, high |
| 3 | New numeric claim "売上200%アップ" (sales up 200%) | Without a substantiation note: needs substantiation, 25, medium. With a note on the original: lowered to info, 5, low |
| 4 | Landing link points to another domain | Shown as the top difference (critical), 50, high |
| 5 | Required disclaimer missing / banned brand word added | Brand rule detection, 25, medium (each) |
| 6 | "必ず痩せる" (guaranteed weight loss) in the health category | Category list hit, 75, high. The same copy in the food category does not hit that list (25, medium) |
| 7 | Full-width "ｎｏ．１" and "Ｇｕａｒａｎｔｅｅｄ" | Detected after normalization (25, medium and 55, high) |
| 8 | No registered original | Classified "no original", no score |
| 9 | Boundaries | 25 → medium, 20 → low, 50 → high, 45 → medium, raw 155 → capped at 100, high |
| 10 | Export from the demo ledger | All 7 file hashes match; verify passes |
| 11 | Timeline | 45 entries from both ledgers in time order; summary counts equal the timeline and creative-check records; 2 proxy-forwarded calls matched to Watch changes |
| 12 | One file edited after export | verify fails (exit code 2); added files, missing files and an edited manifest also fail |

### Tests: 333 passing, no network

| Area | Files | Tests |
|---|---|---|
| Watch + Revert | `test_cli`, `test_client_snapshot`, `test_diff_ledger`, `test_policy`, `test_revert_alerts`, `test_scenarios` | 111 |
| MCP proxy | `test_mock_ads_mcp`, `test_proxy_scenarios`, `test_proxy_policy`, `test_proxy_failclosed`, `test_proxy_approvals`, `test_proxy_audit_mcp` | 117 |
| Creative Check + evidence | `test_creative_scenarios`, `test_creative_rules`, `test_creative_watch`, `test_evidence` | 105 |
| **Total** | | **333** |

The read-only Marketing API client is tested with `httpx.MockTransport`. MCP is tested with in-memory sessions and a
local stdio subprocess. CI runs the suite and all four demos on Python 3.12 / Ubuntu
([.github/workflows/ci.yml](.github/workflows/ci.yml)).

## Policy examples

Rules live in `policy.yaml`, not in code. These are excerpts from
[configs/policy.proxy.example.yaml](configs/policy.proxy.example.yaml); IDs are fictional.

```yaml
version: 1
timezone: Asia/Tokyo

rules:
  # Budget cap. Values are the integers the API returns (yen for JPY, cents for USD).
  # Reaching the cap exactly is allowed. Proxy: deny. Watch: violation.
  - id: adset-daily-cap
    type: budget_cap
    entity_types: [adset]
    fields: [daily_budget]
    max: 20000

  # One change may raise a budget by at most +50% of its current value.
  # Above that, a person approves. No current value -> approval (fail-closed).
  - id: budget-step-up
    type: budget_increase_ratio
    entity_types: [adset, campaign]
    fields: [daily_budget, lifetime_budget]
    max_increase_pct: 50

  # The landing URL may not change, including by swapping the ad's creative.
  # Proxy: deny. Watch: violation (and an ad vanishing is flagged too).
  - id: frozen-landing-url
    type: frozen_fields
    entity_types: [ad]
    fields: [creative_resolved.link]
    include_deletion: true

  # Writes outside these hours need approval at the proxy; Watch marks them as warnings.
  # start is inclusive, end is exclusive; overnight windows such as 22:00-06:00 work.
  - id: business-hours
    type: change_window
    windows:
      - {days: [mon, tue, wed, thu, fri], start: "09:00", end: "19:00"}

proxy:
  # Passed through without evaluation (still audited). Anything else is treated as a write;
  # unknown tools are denied. Downstream annotations such as readOnlyHint are not trusted.
  read_tools: [ads_get_ad_accounts, ads_get_ad_entities, ads_get_creatives]
  approval_ttl_hours: 24
```

Each rule can set `on_proxy: deny | require_approval` to override its default at the proxy. Actor labels come only
from declared application or actor IDs in the `attribution` section; anything undeclared stays `unknown`.

## Architecture

```
Before execution (proxy)
  AI agent --MCP stdio--> Rein proxy --------------------------> mock Ads MCP (in memory)
                          interpret call, read current value      only "allow" and approved
                          policy -> allow / approval / deny        calls are forwarded
                              |
                              +--> proxy_audit, approvals ----------------------+
                                                                                |
After the fact (Watch + Revert)                                                 v
  snapshots (fixtures or read-only API) -> normalize -> diff -> attribution -> SQLite ledger
  activity history ------------------------------------------------^              |
                                                                                 +--> policy -> alerts
                                                                                 +--> revert plan -> mock client only
Creatives                                                                        |
  new / changed creatives -> match original -> diff, claims, rules -> score --->+  (creative_checks)

Evidence
  ledger -> timeline.json / timeline.csv, creative_checks.json, policy/ copies, summary.md
         -> manifest.json (sha256 of every file) -> rein evidence verify
```

| Module | Role |
|---|---|
| [src/rein/client.py](src/rein/client.py) | `AdsReader` / `AdsWriter` interfaces; fixture-backed mock client (read and write); read-only Marketing API client (GET only, token from `REIN_META_ACCESS_TOKEN`) |
| [src/rein/snapshot.py](src/rein/snapshot.py), [diff.py](src/rein/diff.py) | Snapshot normalization (NFKC) and field-level diff |
| [src/rein/attribution.py](src/rein/attribution.py) | Matches changes to activity history; labels an actor only when it is declared in policy |
| [src/rein/ledger.py](src/rein/ledger.py) | SQLite ledger: snapshots, changes, alerts, revert runs, proxy audit, approvals, creative originals and checks |
| [src/rein/policy.py](src/rein/policy.py) | Loads `policy.yaml` and evaluates rules for Watch and for the proxy |
| [src/rein/revert.py](src/rein/revert.py), [alerts.py](src/rein/alerts.py), [pipeline.py](src/rein/pipeline.py) | Revert planning (execution refuses anything but the mock), alert sinks, ingest and check |
| [src/rein/proxy.py](src/rein/proxy.py), [mock_ads_mcp.py](src/rein/mock_ads_mcp.py) | MCP proxy (stdio server) and the mock downstream. Tool names follow Meta's published Ads MCP tool list; argument shapes are Rein's assumptions |
| [src/rein/creative.py](src/rein/creative.py) | Creative Check: originals registry, lineage matching, diff, claims, rules, score, Watch integration |
| [src/rein/evidence.py](src/rein/evidence.py) | Evidence pack export and verify |
| [src/rein/cli.py](src/rein/cli.py) | `rein snapshot · feed · check · revert · proxy · approvals · audit · creative · evidence` |

More detail (CLI, ledger columns, attribution rules, evidence pack files, how Rein departs from the official
documentation) is in [docs/reference.md](docs/reference.md).

## Limitations

- **Not connected to a real account.** Everything is tested on fixtures and a mock Ads MCP server. Real use needs
  an HTTP downstream for the real Ads MCP (OAuth or Bearer), the right permissions (`ads_read` / `ads_management`, plus
  what Meta requires for Ads MCP access), App Review and business verification. The proxy's call interpretation must
  also be fitted to the real `tools/list` schemas, and the real activity history may show proxied changes differently
  from the fixtures.
- **Writes to real accounts are disabled by design.** The real client has no write methods; revert execution refuses
  anything but the mock client.
- **Self-declared identities.** The agent's name comes from MCP `clientInfo` and is recorded as declared, not
  verified. Approvers and the people who register creative originals are self-declared too. There is no approval UI
  and no approver authentication.
- **TOCTOU on the allow path.** The account can change between the proxy's evaluation of a call and the moment the
  downstream executes it.
- **The proxy controls only what goes through it.** Changes made by Meta's own AI features, by people in Ads Manager
  or by agents that bypass the proxy can only be detected and reverted afterwards.
- **Attribution only goes as far as the activity history.** Changes with no matching, declared activity are
  `unknown`; Rein does not guess. Change times are known only to the snapshot interval.
- **Claim extraction is declared patterns only.** Reworded claims, text inside images and video audio are not
  detected.
- **Scores are not compliance judgements.** The Creative Check score is a review-priority aid, not a pass / fail and
  not legal advice.
- **The rule sets are starters.** The prohibited-term, substantiation and brand lists are small examples, not
  comprehensive lists, and have not been through legal review.
- **Evidence hashes detect changes after export only.** They do not prove the ledger was untouched before export.
  The ledger itself has no append-only log, external timestamping or signing. The `policy/` folder holds the files
  given at export time; the ledger does not record which policy was active when.

## License

Proprietary — Copyright (c) 2026 Yuta Nagao, all rights reserved.
This repository is public for evaluation only; no licence to use, copy,
modify, or distribute is granted. The project is available for
acquisition or licensing — contact via
https://nagayu-portfolio.nagayu.workers.dev/en

## Contact

For acquisition or licensing enquiries: https://nagayu-portfolio.nagayu.workers.dev/en
