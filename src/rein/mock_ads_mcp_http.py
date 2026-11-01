"""Mock Ads MCP server over streamable HTTP (localhost only). THIS IS A MOCK, not Meta's Ads MCP.

Same fixture-backed tools, in-memory writes and optional `--state` file as the stdio mock
(`rein.mock_ads_mcp`); only the transport differs. Every request must carry an
`Authorization: Bearer <anything>` header: any non-empty value is accepted locally, a missing or
empty one gets 401. The token value is never logged or stored.

Faults (off by default, for tests and diagnosis drills; none of them is Meta's real wording):
  jsonrpc-errors    tool errors come back as JSON-RPC errors instead of isError results
  write-iserror     write tools answer isError: true with an error message; nothing changes
  write-error-body  write tools answer a transport-successful result whose content is {"error": ...}; nothing changes
  echo-token        every tool call fails with a JSON-RPC error that echoes the Authorization header

Run: python -m rein.mock_ads_mcp_http --fixture fixtures/base [--state mock_state.json] [--port 8765] [--fault ...]
"""

from __future__ import annotations

import argparse
import contextlib
import json
import socket
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Iterator

import uvicorn
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.shared.exceptions import McpError
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from .mock_ads_mcp import WRITE_TOOLS, MockAdsMcp, build_server

MCP_PATH = "/mcp"
HOST = "127.0.0.1"


def _has_bearer(scope: Scope) -> bool:
    for key, value in scope.get("headers", []):
        if key == b"authorization":
            scheme, _, credential = value.decode("latin-1").partition(" ")
            return scheme.lower() == "bearer" and bool(credential.strip())
    return False


class _BearerGate:
    """ASGI wrapper: 401 without a Bearer header, otherwise hand over to the MCP session manager."""

    def __init__(self, manager: StreamableHTTPSessionManager):
        self.manager = manager

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not _has_bearer(scope):
            response = JSONResponse({"error": "missing bearer token"}, status_code=401,
                                    headers={"WWW-Authenticate": "Bearer"})
            await response(scope, receive, send)
            return
        await self.manager.handle_request(scope, receive, send)


FAULTS = ("jsonrpc-errors", "write-iserror", "write-error-body", "echo-token")


def _text_result(text: str, is_error: bool) -> types.ServerResult:
    return types.ServerResult(types.CallToolResult(content=[types.TextContent(type="text", text=text)],
                                                   isError=is_error))


def _apply_faults(server: Server, faults: frozenset[str]) -> None:
    original = server.request_handlers[types.CallToolRequest]

    async def handler(req: types.CallToolRequest) -> types.ServerResult:
        name = req.params.name
        if "echo-token" in faults:
            request = getattr(server.request_context, "request", None)
            auth = request.headers.get("authorization", "") if request is not None else ""
            raise McpError(types.ErrorData(code=-32001, message=f"(mock) invalid OAuth access token: {auth}"))
        if name in WRITE_TOOLS and "write-iserror" in faults:
            return _text_result(f"Error: (mock) (#100) invalid parameter for {name}; nothing was created", True)
        if name in WRITE_TOOLS and "write-error-body" in faults:
            body = {"error": {"message": "(mock) (#2635) the request could not be completed", "code": 100,
                              "type": "OAuthException"}}
            return _text_result(json.dumps(body), False)
        result = await original(req)
        if "jsonrpc-errors" in faults and getattr(result.root, "isError", False):
            text = " ".join(c.text for c in result.root.content if isinstance(c, types.TextContent))
            raise McpError(types.ErrorData(code=-32602, message=text.removeprefix("error: ")))
        return result

    server.request_handlers[types.CallToolRequest] = handler


def build_http_app(mock: MockAdsMcp, faults: tuple[str, ...] | frozenset[str] = ()) -> Starlette:
    unknown = set(faults) - set(FAULTS)
    if unknown:
        raise ValueError(f"unknown mock faults: {sorted(unknown)}")
    server = build_server(mock)
    if faults:
        _apply_faults(server, frozenset(faults))
    manager = StreamableHTTPSessionManager(app=server)

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        async with manager.run():
            yield

    return Starlette(routes=[Route(MCP_PATH, endpoint=_BearerGate(manager), methods=["GET", "POST", "DELETE"])],
                     lifespan=lifespan)


@contextlib.contextmanager
def serve_in_thread(mock: MockAdsMcp, port: int = 0, faults: tuple[str, ...] = ()) -> Iterator[str]:
    """Serve the mock on 127.0.0.1 in a background thread; yields the endpoint URL."""
    with serve_app_in_thread(build_http_app(mock, faults), port) as url:
        yield url


@contextlib.contextmanager
def serve_app_in_thread(app: Any, port: int = 0) -> Iterator[str]:
    """Serve any ASGI app on 127.0.0.1 in a background thread; yields http://127.0.0.1:<port>/mcp."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((HOST, port))
    url = f"http://{HOST}:{sock.getsockname()[1]}{MCP_PATH}"
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False, lifespan="auto",
                                           timeout_graceful_shutdown=1))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("mock Ads MCP (HTTP) did not start")
        time.sleep(0.01)
    try:
        yield url
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="rein-mock-ads-mcp-http",
                                     description="Mock Ads MCP server (streamable HTTP on 127.0.0.1). Not Meta.")
    parser.add_argument("--fixture", required=True)
    parser.add_argument("--state", help="persist mock state to this JSON file")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--fault", action="append", choices=FAULTS, default=[], help="simulate a failure mode")
    parser.add_argument("--draft-lifecycle", action="store_true",
                        help="creates answer DRAFT descriptors that only publish_as_active makes real")
    args = parser.parse_args(argv)
    app = build_http_app(MockAdsMcp(Path(args.fixture), args.state, draft_lifecycle=args.draft_lifecycle),
                         tuple(args.fault))
    print(f"mock Ads MCP (HTTP): http://{HOST}:{args.port}{MCP_PATH}  (not Meta; Bearer header required)")
    uvicorn.run(app, host=HOST, port=args.port, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
