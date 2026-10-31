"""Marketing API clients.

- `AdsReader`: read-only interface used by snapshots.
- `AdsWriter`: write interface used by revert execution. Only `MockAdsClient` implements it.
- `MetaGraphReadClient`: real Graph API client. Read-only by construction: it has no write
  methods and its transport rejects any non-GET request.
"""

from __future__ import annotations

import copy
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlparse

import httpx

from .timeutil import now_utc, parse_time

ENTITY_TYPES = ("campaign", "adset", "ad", "creative")

# Ad account edges (GET /act_{id}/{edge}).
EDGES = {
    "campaign": "campaigns",
    "adset": "adsets",
    "ad": "ads",
    "creative": "adcreatives",
}

FIELDS = {
    "campaign": [
        "id", "name", "status", "effective_status", "objective", "daily_budget",
        "lifetime_budget", "spend_cap", "bid_strategy", "special_ad_categories", "updated_time",
    ],
    "adset": [
        "id", "name", "campaign_id", "status", "effective_status", "daily_budget",
        "lifetime_budget", "bid_amount", "bid_strategy", "billing_event", "optimization_goal",
        "targeting", "start_time", "end_time", "updated_time",
    ],
    "ad": [
        "id", "name", "adset_id", "campaign_id", "status", "effective_status", "creative{id}",
        "updated_time",
    ],
    "creative": [
        "id", "name", "status", "object_story_spec", "asset_feed_spec", "body", "title",
        "link_url", "call_to_action_type",
    ],
}

ACTIVITY_FIELDS = [
    "event_time", "event_type", "translated_event_type", "actor_id", "actor_name",
    "application_id", "application_name", "object_id", "object_name", "object_type",
    "extra_data", "date_time_in_timezone",
]

DEFAULT_API_VERSION = "v26.0"
GRAPH_HOST = "graph.facebook.com"
TOKEN_ENV = "REIN_META_ACCESS_TOKEN"
API_VERSION_ENV = "REIN_META_API_VERSION"


class ClientError(RuntimeError):
    pass


class WriteBlockedError(ClientError):
    """Raised when anything tries to send a non-GET request through the real client."""


class MetaApiError(ClientError):
    pass


class MockWriteError(ClientError):
    pass


@runtime_checkable
class AdsReader(Protocol):
    api_version: str
    source: str

    def account_id(self) -> str: ...

    def list_entities(self, entity_type: str) -> list[dict[str, Any]]: ...

    def list_activities(self) -> list[dict[str, Any]]: ...

    def observed_at(self) -> datetime: ...


@runtime_checkable
class AdsWriter(Protocol):
    def get_entity(self, entity_type: str, entity_id: str) -> dict[str, Any] | None: ...

    def update(self, entity_type: str, entity_id: str, params: dict[str, Any]) -> None: ...


# --------------------------------------------------------------------------- mock


def _load_edge(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    # Recorded responses are Graph-shaped: {"data": [...], "paging": {...}}.
    if isinstance(payload, dict):
        return list(payload.get("data", []))
    return list(payload)


class MockAdsClient:
    """Serves recorded responses from a fixture directory and accepts writes in memory.

    Fixture layout: meta.json, campaigns.json, adsets.json, ads.json, adcreatives.json,
    activities.json (optional).
    """

    source = "mock"

    def __init__(self, fixture_dir: str | Path):
        self.fixture_dir = Path(fixture_dir)
        meta = json.loads((self.fixture_dir / "meta.json").read_text(encoding="utf-8"))
        self._account_id = meta["account_id"]
        self.api_version = meta.get("api_version", DEFAULT_API_VERSION)
        self._captured_at = parse_time(meta["captured_at"])
        self.source = f"mock:{self.fixture_dir.as_posix()}"
        self._state = {t: _load_edge(self.fixture_dir / f"{EDGES[t]}.json") for t in ENTITY_TYPES}
        self._activities = _load_edge(self.fixture_dir / "activities.json")
        self.write_log: list[dict[str, Any]] = []

    def account_id(self) -> str:
        return self._account_id

    def list_entities(self, entity_type: str) -> list[dict[str, Any]]:
        return copy.deepcopy(self._state[entity_type])

    def list_activities(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self._activities)

    def observed_at(self) -> datetime:
        return self._captured_at

    def get_entity(self, entity_type: str, entity_id: str) -> dict[str, Any] | None:
        for row in self._state[entity_type]:
            if str(row.get("id")) == str(entity_id):
                return copy.deepcopy(row)
        return None

    def update(self, entity_type: str, entity_id: str, params: dict[str, Any]) -> None:
        """Apply a Graph-style update (POST /{id}) to the in-memory state."""
        target = next((r for r in self._state[entity_type] if str(r.get("id")) == str(entity_id)), None)
        if target is None:
            raise MockWriteError(f"{entity_type} {entity_id} not found")
        for key, value in params.items():
            if entity_type == "ad" and key == "creative":
                creative_id = str(value["creative_id"])
                if self.get_entity("creative", creative_id) is None:
                    raise MockWriteError(f"creative {creative_id} not found")
                target["creative"] = {"id": creative_id}
            else:
                target[key] = value
        self.write_log.append({"entity_type": entity_type, "entity_id": str(entity_id), "params": copy.deepcopy(params)})


# --------------------------------------------------------------------------- real (read-only)


class ReadOnlyTransport(httpx.BaseTransport):
    """Wraps a transport and refuses every request that is not a GET."""

    def __init__(self, inner: httpx.BaseTransport):
        self._inner = inner

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if request.method != "GET":
            raise WriteBlockedError(f"Phase 1 is read-only: refused {request.method} {request.url.path}")
        return self._inner.handle_request(request)


class MetaGraphReadClient:
    """Reads an ad account through the Marketing API. Has no write methods by design.

    Assumes a token with read access to ads (e.g. `ads_read`). The token is taken only from
    the environment and is sent as a header, never in a URL.
    """

    source = "meta_graph"

    def __init__(
        self,
        account_id: str,
        *,
        token: str | None = None,
        api_version: str | None = None,
        transport: httpx.BaseTransport | None = None,
        page_limit: int = 100,
        timeout: float = 30.0,
    ):
        token = token or os.environ.get(TOKEN_ENV)
        if not token:
            raise ClientError(f"{TOKEN_ENV} is not set")
        if not account_id.startswith("act_"):
            account_id = f"act_{account_id}"
        self._account_id = account_id
        self.api_version = api_version or os.environ.get(API_VERSION_ENV) or DEFAULT_API_VERSION
        self._page_limit = page_limit
        self._http = httpx.Client(
            base_url=f"https://{GRAPH_HOST}/{self.api_version}/",
            headers={"Authorization": f"Bearer {token}"},
            transport=ReadOnlyTransport(transport or httpx.HTTPTransport()),
            timeout=timeout,
        )
        self._observed_at: datetime | None = None

    def account_id(self) -> str:
        return self._account_id

    def observed_at(self) -> datetime:
        return self._observed_at or now_utc()

    def close(self) -> None:
        self._http.close()

    def _get_all(self, path: str, fields: list[str]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        response = self._http.get(path, params={"fields": ",".join(fields), "limit": self._page_limit})
        while True:
            payload = self._decode(response)
            rows.extend(payload.get("data", []))
            next_url = payload.get("paging", {}).get("next")
            if not next_url:
                break
            # Only follow pagination on the Graph host so the auth header never leaves it.
            if urlparse(next_url).hostname != GRAPH_HOST:
                raise MetaApiError(f"refusing to follow paging.next to unexpected host: {urlparse(next_url).hostname}")
            response = self._http.get(next_url)
        self._observed_at = now_utc()
        return rows

    @staticmethod
    def _decode(response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise MetaApiError(f"non-JSON response (HTTP {response.status_code})") from exc
        if response.status_code >= 400 or "error" in payload:
            err = payload.get("error", {})
            raise MetaApiError(
                f"Graph API error HTTP {response.status_code}: code={err.get('code')} "
                f"subcode={err.get('error_subcode')} message={err.get('message')}"
            )
        return payload

    def list_entities(self, entity_type: str) -> list[dict[str, Any]]:
        return self._get_all(f"{self._account_id}/{EDGES[entity_type]}", FIELDS[entity_type])

    def list_activities(self) -> list[dict[str, Any]]:
        # The activities edge documents no since/until parameters and returns about one
        # week of history by default; time filtering is done on our side.
        return self._get_all(f"{self._account_id}/activities", ACTIVITY_FIELDS)
