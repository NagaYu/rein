# fixtures

All data here is fictional: businesses, brands, IDs, amounts, people and app names. URLs use the reserved
`example.com` / `example.net` domains. IDs follow obviously synthetic patterns (a fixed prefix, a run of zeros and a
small number). The ad copy is Japanese because the fictional ads are.

Regenerate everything with `scripts/build_fixtures.py`. If you edit a file by hand, change the generator as well.

## Watch + Revert

- `base/`: recorded responses for a small ad account, in Graph API `{"data": [...]}` form.
- `scenarios/<name>/before`, `after`: before and after each planted scenario. `after/activities.json` is the activity history.
- `scenarios/<name>/scenario.json`: the expected findings, read by the tests.

| Scenario | What happens | Expected |
|---|---|---|
| 1_budget_spike | Ad set daily budget 5000 → 50000 | `budget_cap` violation, actor external_agent |
| 2_url_swap | Creative swap moves the ad's link to another domain | `frozen_fields` violation, no history so actor unknown |
| 3_prohibited_term | Swapped copy contains "ｎｏ．１" and "絶対" ("absolutely") | `prohibited_terms` violation, actor meta_ai (declared) |
| 4_off_hours | Ad set paused Wednesday 03:12 JST | `change_window` warning, actor human |
| 5_normal_change | Weekday 13:00 budget 5000 → 8000 | No finding |

## proxy/

`proxy/scenarios.json`: the MCP proxy scenarios. The mock Ads MCP downstream starts from `base/`. `calls` (1–10)
hold the call, the expected decision and whether the downstream changes; `approval_flows` (11–14) hold the expected
approval outcomes. Read by `tests/test_proxy_scenarios.py` and `scripts/demo_proxy.py`.

## creative/ and evidence/

- `creative/base/`, `creative/after/`: a second fictional account, `act_100000000000002`, for Creative Check
  (generated). In `after`, two AI rewrites are swapped onto ads (one with meta_ai history, one with none), and one new
  creative is attached to no ad.
- `creative/originals.json`: the approved originals (registrant self-declared) and a substantiation note.
- `creative/attributes.json`: industry category and brand per ad set (Meta has no such fields).
- `creative/cases.json`: Creative Check scenarios 1–9. Each candidate is a copy of a base creative with a
  `link_data` override. Read by `tests/test_creative_scenarios.py`.
- `evidence/after/`: account 1 the next day, for the evidence demo. It contains the two budget changes the proxy
  forwarded (no history) and changes by an external agent, a person, Meta AI and an unknown actor (generated).
