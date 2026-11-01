"""`rein preflight`: check that a real read-only Watch run is configured, without touching the network.

Offline checks (always): the Marketing API token variable the read-only client uses
(REIN_META_ACCESS_TOKEN) is set, the account id has the form act_<digits>, and the optional API
version override looks like vNN.N. Output shows the token only as present/absent and its length.

The connectivity check is a separate step (`--live`), meant for the owner to run by hand later
against a test ad account. It makes read-only GET requests through the existing read-only client
(which refuses anything but GET). Tests run it only against an in-process mock transport.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from .client import API_VERSION_ENV, DEFAULT_API_VERSION, TOKEN_ENV, ClientError, MetaGraphReadClient
from .downstream_http import TOKEN_ENV as ADS_MCP_TOKEN_ENV

ACCOUNT_RE = re.compile(r"^act_\d{1,20}$")
VERSION_RE = re.compile(r"^v\d{1,3}\.\d{1,2}$")


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    required: bool = True


@dataclass
class PreflightResult:
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks if c.required)


def _token_state(env: dict[str, str], name: str) -> tuple[bool, str]:
    value = env.get(name, "").strip()
    return (True, f"present (length {len(value)})") if value else (False, "absent")


def offline_preflight(account: str | None, env: dict[str, str] | None = None) -> PreflightResult:
    env = dict(os.environ if env is None else env)
    result = PreflightResult()
    ok, detail = _token_state(env, TOKEN_ENV)
    result.checks.append(Check(f"token {TOKEN_ENV}", ok, detail if ok else f"absent: export {TOKEN_ENV} in your shell"))
    if account is None:
        result.checks.append(Check("account", False, "missing: pass --account act_<digits>"))
    elif ACCOUNT_RE.fullmatch(account):
        result.checks.append(Check("account", True, account))
    else:
        result.checks.append(Check("account", False, f"malformed {account!r}: expected act_<digits>"))
    version = env.get(API_VERSION_ENV, "").strip()
    if not version:
        result.checks.append(Check(f"api version {API_VERSION_ENV}", True, f"unset (default {DEFAULT_API_VERSION})"))
    elif VERSION_RE.fullmatch(version):
        result.checks.append(Check(f"api version {API_VERSION_ENV}", True, version))
    else:
        result.checks.append(Check(f"api version {API_VERSION_ENV}", False, "malformed: expected vNN.N"))
    ok, detail = _token_state(env, ADS_MCP_TOKEN_ENV)
    result.checks.append(Check(f"token {ADS_MCP_TOKEN_ENV} (proxy HTTP downstream)", ok,
                               detail if ok else "absent (only needed for the proxy's HTTP downstream)",
                               required=False))
    return result


def connectivity_check(account: str, *, transport: httpx.BaseTransport | None = None,
                       env: dict[str, str] | None = None) -> Check:
    """The gated live step: read-only GETs for the account's campaigns. Not run by tests against Meta."""
    env = dict(os.environ if env is None else env)
    token = env.get(TOKEN_ENV, "").strip()
    if not token or not ACCOUNT_RE.fullmatch(account):
        return Check("connectivity", False, "not attempted: the offline checks must pass first")
    try:
        client = MetaGraphReadClient(account, token=token, api_version=env.get(API_VERSION_ENV) or None,
                                     transport=transport)
        try:
            campaigns = client.list_entities("campaign")
        finally:
            client.close()
    except ClientError as exc:
        message = str(exc).replace(token, "[redacted]") if token else str(exc)
        return Check("connectivity", False, f"read failed: {type(exc).__name__}: {message}")
    except httpx.HTTPError as exc:
        return Check("connectivity", False, f"read failed: {type(exc).__name__}")
    return Check("connectivity", True, f"read-only GET ok, {len(campaigns)} campaign(s) visible")


def render(result: PreflightResult, *, live_note: bool = True) -> str:
    lines = ["rein preflight (read-only Watch)"]
    for c in result.checks:
        mark = "ok  " if c.ok else ("FAIL" if c.required else "info")
        lines.append(f"  [{mark}] {c.name}: {c.detail}")
    if live_note and not any(c.name == "connectivity" for c in result.checks):
        lines.append("  [skip] connectivity: not checked. Run `rein preflight --account ... --live` yourself when "
                     "you are ready; it makes read-only GET requests to the Marketing API.")
    lines.append("  result: " + ("ready" if result.ok else "not ready"))
    return "\n".join(lines)


def as_dict(result: PreflightResult) -> dict[str, Any]:
    return {"ok": result.ok, "checks": [c.__dict__ for c in result.checks]}
