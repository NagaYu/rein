"""Helpers for the HTTP downstream tests: local servers on 127.0.0.1 only, the dummy token only."""

from __future__ import annotations

import asyncio
import contextlib
import socket
from typing import Iterator

import httpx
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from rein.mock_ads_mcp import MockAdsMcp
from rein.mock_ads_mcp_http import serve_app_in_thread, serve_in_thread
from proxy_helpers import BASE

DUMMY = "test-token-not-real"
ENV = {"REIN_ADS_MCP_TOKEN": DUMMY}


@contextlib.contextmanager
def http_mock(fixture=BASE, faults: tuple[str, ...] = ()) -> Iterator[tuple[str, MockAdsMcp]]:
    mock = MockAdsMcp(fixture)
    with serve_in_thread(mock, faults=faults) as url:
        yield url, mock


def misbehaving_app(mode: str) -> Starlette:
    """A local endpoint that fails in one specific way."""

    async def endpoint(request):
        if mode == "slow":
            await asyncio.sleep(30)
        if mode == "garbage-json":
            return Response(b"{this is not json", media_type="application/json")
        if mode == "html":
            return Response(b"<html>maintenance</html>", media_type="text/html")
        if mode == "500":
            return JSONResponse({"error": "boom"}, status_code=500)
        if mode == "500-long":
            lines = "\n".join(f"line {i}: Authorization: Bearer {DUMMY} trace frame {i}" for i in range(80))
            return Response(lines.encode(), status_code=500, media_type="text/plain")
        if mode == "401":
            return JSONResponse({"error": "unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})
        if mode == "403":
            return JSONResponse({"error": "forbidden"}, status_code=403)
        return JSONResponse({}, status_code=404)

    return Starlette(routes=[Route("/mcp", endpoint=endpoint, methods=["GET", "POST", "DELETE"])])


@contextlib.contextmanager
def misbehaving(mode: str) -> Iterator[str]:
    with serve_app_in_thread(misbehaving_app(mode)) as url:
        yield url


def closed_port_url() -> str:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return f"http://127.0.0.1:{port}/mcp"


class StripAuthorization(httpx.AsyncBaseTransport):
    def __init__(self):
        self.inner = httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        del request.headers["authorization"]
        return await self.inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self.inner.aclose()
