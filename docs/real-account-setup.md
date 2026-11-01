# Real-account setup: the owner's checklist

This page lists what a person has to do before Rein touches a real Meta ad account, and the order in which the
first live steps should happen. Rein does none of this itself. The owner has run steps 1–5c once, on a
business-owned test account with no payment method (2026-10-31 / 2026-11-01). Sections corrected from that run are
marked **Verified 2026-10-31 / 2026-11-01**. The results are in [phase5-live-findings.md](phase5-live-findings.md).

How to read it:

- **Verified in this repository:** the Rein side of every step. The commands exist, refuse to start without
  their environment variables, and have been tested offline against local mocks.
- **Assumed:** everything on Meta's side. That covers the console flows, permission names as they appear today,
  how the real Ads MCP's tools and arguments look, and how proxied changes show up in the activity history. Menus
  and labels change, so where this page says "verify in the console", check the current Meta developer and
  business documentation rather than following remembered clicks.

## 1. Developer account, app, Business Manager and business verification

- **Meta developer account and an app of the Business type.** Live API access is granted to an app, not to a
  person. Rein's tokens will belong to this app.
- **A Business Manager (business portfolio)** that owns the app and the ad accounts. System users, which are needed
  for long-lived tokens (step 3), live in the business, not in the app.
- **Business verification.** Meta requires it before an app can get advanced access to business permissions and
  before it can work with accounts beyond the developer's own. Expect it to take time. Verify the current
  requirements in the console.

## 2. A test ad account as the only target

**Verified 2026-10-31 / 2026-11-01** (this section replaces the earlier "test ad account attached to the app"
wording).

- **A sandbox ad account does not work for this.** A sandbox ad account is an app-owned asset. It does not appear in
  Business Settings and cannot be assigned to a system user. A system-user token then sees nothing: the Ads MCP
  account list comes back empty, and the Marketing API answers with error #200 (permissions).
- **What works: a business-owned ad account.** Create a new ad account inside the business portfolio and use it only
  for Rein's first runs. Two things to know first:
  - an ad account added to a portfolio cannot be removed from it again, only closed;
  - no payment method is needed for read and write verification. Leaving it without one also means nothing can
    spend.
- Assign **that** account to the system user (section 3) with full access. For every first run, make it the only
  account the token can reach.
- **No production account is touched on this track.** Rein's proxy controls only what passes through it, its
  argument interpretation has not been fitted to the real tool schemas yet, and the activity-history attribution of
  proxied changes is unknown (step 5d). A mistake on a test account costs nothing. The same mistake on a client
  account costs money and trust.
- Give the test account a test campaign with a small, fixed budget, so the one controlled write in step 5c has an
  obvious target.

## 3. System user and token: read-only first

**Verified 2026-10-31 / 2026-11-01** (asset assignment and token facts).

- In the business, create a **system user with the Employee role**. Employee-role system users are the supported
  token kind. Assign it to the app, and to the business-owned test ad account from section 2 with full access. That
  assignment is what clears Marketing API error #200 and makes the account appear in the Ads MCP account list.
- **`ads_mcp_management` appears only after the app has the "Ads MCP" use case added.** It is required for the Ads
  MCP endpoint. Add the use case to the app first, then generate the token.
- Generate a token for that system user with **`ads_mcp_management` + `ads_read`**. This is the read-only posture:
  enough to run `rein proxy discover` and the Watch snapshots, nothing more.
- **Token lifetime:** the token wizard offers 60 days or one-time only; there is no never-expiring option. Note the
  expiry date and plan the rotation (section 4).
- **`ads_management`** (writes through the Marketing API) and **App Review / advanced access** come later, and only
  when they are actually needed for real client accounts (step 5e). Development-mode access on your own test
  account comes first. Do not request review in advance "just in case".
- Whether the single controlled write in step 5c needs more than `ads_mcp_management` on a test account is one of
  the things to verify in the console at that time. **2026-11-01:** with `ads_mcp_management` the server accepted
  a campaign creation, but only as a draft. It is not known whether more permission, or a publish step, would make
  it real.

## 4. Where the tokens go, and how to rotate or revoke them

Rein reads tokens from environment variables and nowhere else.

| Variable | Used by | Purpose |
|---|---|---|
| `REIN_ADS_MCP_TOKEN` | `rein proxy --downstream http`, `rein proxy discover --downstream http`, `rein approvals approve --downstream http` | Bearer token for the Ads MCP endpoint |
| `REIN_META_ACCESS_TOKEN` | `rein snapshot --account`, `rein preflight` | Bearer token for the read-only Marketing API client |

Rules:

- Tokens live **only in the owner's shell environment**. Never put them in the repository, a config file, a
  fixture, a commit, a chat message, an issue or a screenshot. Rein has no flag or file option for a token on
  purpose.
- To set one without leaving it in your shell history, read it from the terminal instead of typing it into a
  command:

  ```bash
  read -rs REIN_META_ACCESS_TOKEN && export REIN_META_ACCESS_TOKEN
  ```

  ```bash
  read -rs REIN_ADS_MCP_TOKEN && export REIN_ADS_MCP_TOKEN
  ```

- `rein preflight` shows a token only as present or absent and its length. If any Rein output ever shows a token
  value, stop and treat it as a bug.
- When you are done, remove the token from the shell:

  ```bash
  unset REIN_ADS_MCP_TOKEN REIN_META_ACCESS_TOKEN
  ```

- **Rotate** by generating a new system-user token, switching the environment variable to it, and then
  invalidating the old one. **Revoke** a token at once if it was ever pasted anywhere it should not be. Revoking
  system-user tokens happens in the business settings for that system user; verify the current location in the
  console. Removing the system user's assignment to the ad account also cuts its access.

## 5. The gated live sequence

Run these in order. **Each step is confirmed separately by the owner at the time it is run.** Passing one step does
not authorize the next. Stop at the first surprise and bring the result back for review before continuing.

Before any of them, check the configuration offline:

```bash
rein preflight --account act_<test account id>
```

**(a) Discovery against the real endpoint.** Read-only: the discovery session can send only `initialize` and
`tools/list`; tool calls are refused in code.

```bash
rein proxy discover --policy configs/policy.proxy.example.yaml --downstream http \
  --downstream-url https://mcp.facebook.com/ads --json --out discovery-report.json
```

Status: done once by the owner (report saved as `discover-live.json`); the interpreter was refitted offline to it
in Phase 5C. Bring `discovery-report.json` back for an interpreter-refit phase. It lists which tools Rein would pass through,
evaluate or deny, and where Rein's assumed argument shapes differ from the real schemas. The report contains no
credentials. Until the refit is done, assume the proxy denies or misreads real calls.

**(a2) First reads through the proxy.** One allowlisted read per command, sent by `scripts/proxy_call.py`
through a `rein proxy` child process (decided and audited like an agent's call; non-read tools are refused unless
`--allow-write` is given):

```bash
.venv/bin/python scripts/proxy_call.py --db rein-test.db --policy configs/policy.proxy.example.yaml \
  --downstream http --downstream-url https://mcp.facebook.com/ads --tool ads_get_ad_accounts
```

```bash
.venv/bin/python scripts/proxy_call.py --db rein-test.db --policy configs/policy.proxy.example.yaml \
  --downstream http --downstream-url https://mcp.facebook.com/ads --tool ads_get_ad_entities \
  --args '{"ad_account_id": "act_<test account id>"}'
```

Check the response shape (Rein's lookups expect `{"data": [...]}` with the requested fields) and the audit with
`rein --db rein-test.db audit`.

**(b) Read-only Watch against the test account.**

```bash
rein preflight --account act_<test account id> --live
```

```bash
rein --db rein-test.db snapshot --account act_<test account id> --policy configs/policy.yaml
```

```bash
rein --db rein-test.db feed
```

`--live` makes read-only GET requests. Take two snapshots some time apart and check that the feed shows what
changed in between.

**(c) One controlled write through the proxy on the test account.** For example, a small budget change on the test
campaign, sent by an agent through `rein proxy --downstream http`. Exactly one write: confirm the proxy's decision,
the audit row, and the change in Ads Manager. If the call needs approval, approve it with
`rein approvals approve ... --downstream http`.

**Verified 2026-11-01:** run twice as a campaign creation. The governance loop worked end to end each time
(require_approval → human approval → executed exactly once). However, the Ads MCP's `ads_create_campaign` returned
a **draft** descriptor, and nothing appeared in the account (see the findings). An `executed` row means the server
accepted the call; it does not prove an object exists. **Always follow a write with verification reads:**
`ads_get_ad_entities` with the returned ID through the proxy, and the Marketing API or Ads Manager.

**(d) How that change appears in Ad Activity.** Take the next Watch snapshot and look at the activity entry for the
change: which actor and which application Meta records for a call that came through the proxy. This is the open
attribution question from Phase 2. Until it is answered, Rein's ledger shows such changes as `unknown`, and that is
correct behavior, not a bug.

**Status 2026-11-01: unmeasured.** Nothing materialised in step (c), so there was no change for the activity log
to record.

**(e) Only after (a)–(d): a separate decision** on `ads_management`, App Review and advanced access for real
client accounts. That decision is a business and legal one as much as a technical one. It needs the refit from (a),
the attribution answer from (d), and the items below.

## 6. What this does not give you

- **No approver authentication.** Approver names (`--by`), agent names (MCP `clientInfo`) and the registrants of
  creative originals are self-declared and recorded as `"verified": false`.
- **The allow-path TOCTOU gap remains.** The account can change between the proxy's evaluation of a call and its
  execution downstream.
- **Claim extraction is pattern-only.** Reworded claims, text inside images and video audio are not detected, and
  Creative Check scores are not compliance judgements.
- **Passing these steps on a test account is not production readiness.** It shows the connection works and the
  first assumptions hold on one test account. It does not show the rules are right for a client, that attribution
  is complete, or that the ledger is tamper-resistant (the evidence-pack hashes detect changes after export only).
