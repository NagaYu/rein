from __future__ import annotations

import json

import httpx
import pytest

from rein.client import (
    ENTITY_TYPES,
    AdsReader,
    ClientError,
    MetaApiError,
    MetaGraphReadClient,
    MockAdsClient,
    WriteBlockedError,
)
from rein.snapshot import normalize_entities, normalize_entity, take_snapshot
from rein.timeutil import iso, parse_time

from conftest import FIXTURES


def test_mock_client_reads_base_fixture():
    client = MockAdsClient(FIXTURES / "base")
    assert isinstance(client, AdsReader)
    counts = {t: len(client.list_entities(t)) for t in ENTITY_TYPES}
    assert counts == {"campaign": 2, "adset": 3, "ad": 4, "creative": 4}


def test_snapshot_records_time_version_source():
    snap = take_snapshot(MockAdsClient(FIXTURES / "base"))
    assert snap.account_id == "act_100000000000001"
    assert snap.api_version == "v26.0"
    assert iso(snap.taken_at) == "2026-10-27T00:00:00Z"
    assert snap.source.startswith("mock:")


def test_normalization_casts_budget_strings_and_sorts_keys():
    a = normalize_entity("adset", {"name": "x", "id": 1, "daily_budget": "5000", "end_time": None})
    b = normalize_entity("adset", {"daily_budget": 5000, "id": "1", "name": "x"})
    assert a == b
    assert list(a) == sorted(a)
    assert a["daily_budget"] == 5000 and a["id"] == "1"
    assert "end_time" not in a


def test_normalization_handles_float_strings_and_nested_order():
    a = normalize_entity("adset", {"id": "1", "daily_budget": "5000.0", "targeting": {"age_max": 44, "age_min": 25}})
    b = normalize_entity("adset", {"id": "1", "daily_budget": 5000, "targeting": {"age_min": 25, "age_max": 44}})
    assert a == b


def test_ad_creative_shape_variants_normalize_equal():
    assert normalize_entity("ad", {"id": "1", "creative": "9"}) == normalize_entity("ad", {"id": "1", "creative": {"id": 9}})


def test_ad_exposes_resolved_creative_fields():
    snap = take_snapshot(MockAdsClient(FIXTURES / "base"))
    ad = snap.get("ad", "120000000000000302")
    assert ad["creative_resolved"]["link"] == "https://shop.example.com/autumn/satsumaimo"
    assert ad["creative_resolved"]["call_to_action"] == "SHOP_NOW"


def test_resolve_asset_feed_spec_creative():
    ents = normalize_entities({"creative": [{"id": "5", "asset_feed_spec": {
        "link_urls": [{"website_url": "https://shop.example.com/a"}],
        "bodies": [{"text": "one"}, {"text": "two"}],
        "titles": [{"text": "t"}],
    }}]})
    resolved = ents["creative"]["5"]["resolved"]
    assert resolved == {"link": "https://shop.example.com/a", "message": ["one", "two"], "headline": "t"}


@pytest.mark.parametrize("value,expected", [
    ("2026-10-27T14:05:00+0900", "2026-10-27T05:05:00Z"),
    ("2026-10-27T14:05:00+09:00", "2026-10-27T05:05:00Z"),
    ("2026-10-27T05:05:00Z", "2026-10-27T05:05:00Z"),
    (1793077500, "2026-10-27T05:05:00Z"),
])
def test_parse_time_variants(value, expected):
    assert iso(parse_time(value)) == expected


def test_mock_write_updates_state_and_log():
    client = MockAdsClient(FIXTURES / "base")
    client.update("adset", "120000000000000201", {"daily_budget": 1234})
    assert client.get_entity("adset", "120000000000000201")["daily_budget"] == 1234
    assert client.write_log[-1]["params"] == {"daily_budget": 1234}


# ---------------------------------------------------------------- real client (network-free)


def _graph_transport(seen: list[httpx.Request]):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path.endswith("/campaigns") and "after" not in request.url.params:
            return httpx.Response(200, json={"data": [{"id": "1", "daily_budget": "100"}],
                                             "paging": {"next": "https://graph.facebook.com/v26.0/act_1/campaigns?after=c2"}})
        if path.endswith("/campaigns"):
            return httpx.Response(200, json={"data": [{"id": "2"}]})
        return httpx.Response(200, json={"data": []})
    return httpx.MockTransport(handler)


def test_real_client_requires_token_from_env():
    with pytest.raises(ClientError):
        MetaGraphReadClient("act_1")


def test_real_client_reads_with_pagination_and_header_auth(monkeypatch):
    monkeypatch.setenv("REIN_META_ACCESS_TOKEN", "test-token-not-real")
    seen: list[httpx.Request] = []
    client = MetaGraphReadClient("1", transport=_graph_transport(seen))
    rows = client.list_entities("campaign")
    assert [r["id"] for r in rows] == ["1", "2"]
    assert all(r.method == "GET" for r in seen)
    assert all("access_token" not in str(r.url) for r in seen)
    assert seen[0].headers["authorization"] == "Bearer test-token-not-real"
    assert seen[0].url.path == "/v26.0/act_1/campaigns"
    snap = take_snapshot(client)
    assert snap.entities["campaign"]["1"]["daily_budget"] == 100


def test_real_client_refuses_foreign_paging_host(monkeypatch):
    def handler(request):
        return httpx.Response(200, json={"data": [], "paging": {"next": "https://evil.example.net/steal"}})
    client = MetaGraphReadClient("act_1", token="t", transport=httpx.MockTransport(handler))
    with pytest.raises(MetaApiError):
        client.list_entities("ad")


def test_real_client_surfaces_graph_errors():
    def handler(request):
        return httpx.Response(400, json={"error": {"code": 190, "message": "Invalid OAuth access token"}})
    client = MetaGraphReadClient("act_1", token="t", transport=httpx.MockTransport(handler))
    with pytest.raises(MetaApiError, match="190"):
        client.list_entities("ad")
