"""The downstream mock Ads MCP server (in-process handlers and over the MCP protocol)."""

from __future__ import annotations

import anyio
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from rein.mock_ads_mcp import READ_TOOLS, WRITE_TOOLS, MockAdsMcp, MockToolError, build_server

from conftest import FIXTURES

AS1, AD2, CR1 = "120000000000000201", "120000000000000302", "120000000000000401"
ACCOUNT = "act_100000000000001"


def test_tool_names_follow_meta_ads_mcp_naming():
    # Phase 5C: the mock serves the real tool names in scope (9 known + 6 allowlisted reads).
    assert READ_TOOLS == ("ads_get_ad_accounts", "ads_get_ad_entities", "ads_get_creatives",
                          "ads_account_get_activity_logs", "ads_get_ad_images", "ads_get_ad_videos",
                          "ads_get_custom_audience", "ads_get_ad_account_pages", "ads_get_ig_accounts")
    assert set(WRITE_TOOLS) == {"ads_update_entity", "ads_activate_entity", "ads_create_campaign",
                                "ads_create_ad_set", "ads_create_ad", "ads_create_creative"}


def test_reads_and_updates_in_memory():
    mock = MockAdsMcp(FIXTURES / "base")
    [row] = mock.call("ads_get_ad_entities", {"ad_account_id": "act_100000000000001", "level": "adset", "object_ids": [AS1]})["data"]
    assert row["daily_budget"] == "5000"
    mock.call("ads_update_entity", {"ad_account_id": "act_100000000000001", "entity_id": AS1, "entity_type": "adset", "fields": {"daily_budget": 6000}})
    assert mock.find("adset", AS1)["daily_budget"] == "6000"
    mock.call("ads_update_entity", {"ad_account_id": "act_100000000000001", "entity_id": AD2, "entity_type": "ad", "fields": {"creative_id": CR1}})
    assert mock.find("ad", AD2)["creative"] == {"id": CR1}
    assert len(mock.write_log) == 2


def test_creates_are_paused():
    mock = MockAdsMcp(FIXTURES / "base")
    out = mock.call("ads_create_campaign", {"ad_account_id": ACCOUNT, "buying_type": "AUCTION", "campaign_name": "x", "objective": "OUTCOME_SALES"})
    assert out["status"] == "PAUSED" and mock.find("campaign", out["id"])["status"] == "PAUSED"
    cr = mock.call("ads_create_creative", {"ad_account_id": ACCOUNT, "name": "c", "link_url": "https://shop.example.com/x", "message": "m"})
    ad = mock.call("ads_create_ad", {"ad_account_id": ACCOUNT, "ad_set_id": AS1, "ad_name": "a", "creative": {"creative_id": cr["id"]}})
    assert mock.find("ad", ad["id"])["creative"] == {"id": cr["id"]}


@pytest.mark.parametrize("name,args", [
    ("ads_update_entity", {"ad_account_id": "act_100000000000001", "entity_id": AS1, "entity_type": "adset", "fields": {"daily_budget": "lots"}}),
    ("ads_update_entity", {"ad_account_id": "act_100000000000001", "entity_id": AS1, "entity_type": "adset", "fields": {"targeting": {}}}),
    ("ads_update_entity", {"ad_account_id": "act_100000000000001", "entity_id": "999", "entity_type": "adset", "fields": {"name": "x"}}),
    ("ads_create_campaign", {"ad_account_id": "act_999", "buying_type": "AUCTION", "campaign_name": "x", "objective": "y"}),
    ("ads_delete_entity", {"ad_account_id": "act_100000000000001", "entity_id": AS1, "entity_type": "adset"}),
])
def test_mock_rejects_bad_calls(name, args):
    mock = MockAdsMcp(FIXTURES / "base")
    with pytest.raises(MockToolError):
        mock.call(name, args)
    assert mock.write_log == []


def test_state_file_is_shared_between_instances(tmp_path):
    state = tmp_path / "state.json"
    MockAdsMcp(FIXTURES / "base", state).call("ads_update_entity", {"ad_account_id": "act_100000000000001", "entity_id": AS1, "entity_type": "adset", "fields": {"daily_budget": 7000}})
    assert MockAdsMcp(FIXTURES / "base", state).find("adset", AS1)["daily_budget"] == "7000"


def test_over_mcp_protocol():
    async def scenario():
        mock = MockAdsMcp(FIXTURES / "base")
        async with create_connected_server_and_client_session(build_server(mock)) as session:
            tools = await session.list_tools()
            assert {t.name for t in tools.tools} == set(READ_TOOLS) | set(WRITE_TOOLS)
            res = await session.call_tool("ads_get_ad_entities", {"ad_account_id": "act_100000000000001", "level": "campaign"})
            assert not res.isError and len(res.structuredContent["data"]) == 2
            bad = await session.call_tool("ads_update_entity", {"ad_account_id": "act_100000000000001", "entity_id": AS1, "entity_type": "adset", "fields": {"daily_budget": -1}})
            assert bad.isError
    anyio.run(scenario)
