"""Phase 5A: `rein preflight` — offline checks for the read-only Watch client; the live step is gated."""

from __future__ import annotations

import json

import httpx
import pytest

from rein.cli import main
from rein.preflight import connectivity_check, offline_preflight, render

DUMMY = "test-token-not-real"
ACCOUNT = "act_100000000000001"


def no_network(monkeypatch):
    def refuse(*_a, **_k):
        raise AssertionError("preflight made a network call")

    monkeypatch.setattr(httpx.Client, "send", refuse)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse)


def test_ready_shows_only_presence_and_length():
    result = offline_preflight(ACCOUNT, {"REIN_META_ACCESS_TOKEN": DUMMY})
    text = render(result)
    assert result.ok and f"present (length {len(DUMMY)})" in text and ACCOUNT in text
    assert DUMMY not in text and "connectivity: not checked" in text


@pytest.mark.parametrize("env", [{}, {"REIN_META_ACCESS_TOKEN": ""}, {"REIN_META_ACCESS_TOKEN": "  "}])
def test_missing_token_fails(env):
    result = offline_preflight(ACCOUNT, env)
    assert not result.ok and "absent: export REIN_META_ACCESS_TOKEN" in render(result)


@pytest.mark.parametrize("account", [None, "100000000000001", "act_", "act_12a", "act_1 ", "ACT_1",
                                     "act_123456789012345678901"])
def test_malformed_account_fails(account):
    result = offline_preflight(account, {"REIN_META_ACCESS_TOKEN": DUMMY})
    assert not result.ok
    assert DUMMY not in render(result)


def test_bad_api_version_fails_and_ads_mcp_token_is_informational():
    result = offline_preflight(ACCOUNT, {"REIN_META_ACCESS_TOKEN": DUMMY, "REIN_META_API_VERSION": "26"})
    assert not result.ok
    result = offline_preflight(ACCOUNT, {"REIN_META_ACCESS_TOKEN": DUMMY})
    mcp = next(c for c in result.checks if "REIN_ADS_MCP_TOKEN" in c.name)
    assert not mcp.ok and not mcp.required and result.ok


def test_cli_offline_makes_no_network_call(monkeypatch, capsys):
    no_network(monkeypatch)
    monkeypatch.setenv("REIN_META_ACCESS_TOKEN", DUMMY)
    assert main(["preflight", "--account", ACCOUNT]) == 0
    out = capsys.readouterr().out
    assert "result: ready" in out and DUMMY not in out
    assert main(["preflight", "--account", ACCOUNT, "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True and DUMMY not in json.dumps(data)


def test_cli_failures(monkeypatch, capsys):
    no_network(monkeypatch)
    monkeypatch.delenv("REIN_META_ACCESS_TOKEN", raising=False)
    assert main(["preflight", "--account", ACCOUNT]) == 1
    assert "not ready" in capsys.readouterr().out
    monkeypatch.setenv("REIN_META_ACCESS_TOKEN", DUMMY)
    assert main(["preflight", "--account", "12345"]) == 1
    out = capsys.readouterr().out
    assert "malformed '12345'" in out and DUMMY not in out


def test_cli_live_is_not_attempted_when_offline_checks_fail(monkeypatch, capsys):
    no_network(monkeypatch)
    monkeypatch.delenv("REIN_META_ACCESS_TOKEN", raising=False)
    assert main(["preflight", "--account", ACCOUNT, "--live"]) == 1
    assert "result: not ready" in capsys.readouterr().out  # and no_network() proves nothing was sent


def test_connectivity_check_against_a_mock_transport():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.headers.get("authorization")))
        return httpx.Response(200, json={"data": [{"id": "1"}, {"id": "2"}]})

    check = connectivity_check(ACCOUNT, transport=httpx.MockTransport(handler), env={"REIN_META_ACCESS_TOKEN": DUMMY})
    assert check.ok and "2 campaign(s)" in check.detail
    assert seen[0][0] == "GET" and seen[0][1].endswith(f"/{ACCOUNT}/campaigns") and seen[0][2] == f"Bearer {DUMMY}"


def test_connectivity_errors_are_reported_without_the_token():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"code": 190, "message": f"Invalid OAuth access token {DUMMY}"}})

    check = connectivity_check(ACCOUNT, transport=httpx.MockTransport(handler), env={"REIN_META_ACCESS_TOKEN": DUMMY})
    assert not check.ok and "code=190" in check.detail and DUMMY not in check.detail


def test_connectivity_is_not_attempted_without_offline_prerequisites():
    def handler(request):
        raise AssertionError("should not be called")

    for account, env in ((ACCOUNT, {}), ("12345", {"REIN_META_ACCESS_TOKEN": DUMMY})):
        check = connectivity_check(account, transport=httpx.MockTransport(handler), env=env)
        assert not check.ok and "not attempted" in check.detail
