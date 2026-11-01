"""HTTP downstream for the Rein proxy: an MCP streamable-HTTP endpoint behind a Bearer token.

    agent --stdio--> Rein proxy --streamable HTTP + Authorization: Bearer--> Ads MCP endpoint

- The endpoint URL is configuration (CLI flag). Nothing in code defaults to Meta's endpoint.
- The token comes only from the environment variable REIN_ADS_MCP_TOKEN. With the HTTP
  downstream selected and the variable unset or empty, the proxy refuses to start.
- The token value never goes into audit rows, logs, errors or output: it is only placed in the
  httpx client's headers, failures are reduced to a failure class, and a log-record scrubber
  removes the value if any library ever logs it.
- Each downstream operation (one lookup, one forwarded call, one tools/list) runs in its own MCP
  session: connect, initialize, one request, close. Session ids and protocol-version negotiation
  are left to the official SDK transport; Rein keeps no session state of its own.
- Failures (unreachable, 401/403, other HTTP status, timeout, malformed response, protocol error)
  raise DownstreamFailure with a failure class. The proxy turns that into its existing
  fail-closed outcomes: nothing is executed, lookups count as "current value unavailable".
"""

from __future__ import annotations

import json
import logging
import os
import re
from contextlib import asynccontextmanager, contextmanager
from typing import Any, AsyncIterator, Awaitable, Callable, Iterator, TypeVar
from urllib.parse import urlsplit

import anyio
import httpx
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import McpError
from pydantic import ValidationError

from . import __version__

TOKEN_ENV = "REIN_ADS_MCP_TOKEN"
DEFAULT_TIMEOUT = 30.0
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
REDACTED = "[redacted]"
CLIENT_INFO = types.Implementation(name="rein-proxy", version=__version__)
FAILURE_CLASSES = ("unreachable", "unauthorized", "forbidden", "http_status", "timeout", "malformed",
                   "protocol_error", "error")

T = TypeVar("T")


class DownstreamConfigError(ValueError):
    """The HTTP downstream cannot be configured; the proxy must not start."""


class DownstreamFailure(Exception):
    """A downstream operation failed: a failure class, plus the server's own words as `detail`.

    `detail` is diagnostic text only (single line, at most DETAIL_LIMIT characters, token-scrubbed); it never
    changes a decision. str() of the exception is the class label, so existing outputs stay as they were."""

    def __init__(self, failure_class: str, status: int | None = None, detail: str | None = None):
        self.failure_class = failure_class
        self.status = status
        self.detail = detail
        super().__init__(self.label)

    @property
    def label(self) -> str:
        return f"{self.failure_class} ({self.status})" if self.status else self.failure_class


class _MalformedMessage(Exception):
    pass


class DiscoveryViolation(RuntimeError):
    """Something tried to send a request that read-only discovery does not allow. Never reclassified."""


def token_from_env(env: dict[str, str] | None = None) -> str:
    value = (os.environ if env is None else env).get(TOKEN_ENV, "")
    if not value.strip():
        raise DownstreamConfigError(
            f"{TOKEN_ENV} is not set: refusing to start with an HTTP downstream (fail-closed). "
            f"Export the token in your shell; it is never read from files or flags.")
    return value.strip()


def check_url(url: str) -> str:
    """https for any host; plain http only for loopback (local mocks). No credentials in the URL."""
    if not url:
        raise DownstreamConfigError("an HTTP downstream needs --downstream-url")
    parts = urlsplit(url)
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise DownstreamConfigError("--downstream-url must be an http(s) URL")
    if parts.username or parts.password:
        raise DownstreamConfigError("--downstream-url must not carry credentials; use REIN_ADS_MCP_TOKEN")
    if parts.scheme == "http" and parts.hostname not in LOOPBACK_HOSTS:
        raise DownstreamConfigError("plain http is only allowed for a loopback address; use https")
    return url


def public_url(url: str) -> str:
    """The URL as it may appear in reports: scheme, host, port and path only."""
    parts = urlsplit(url)
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{parts.hostname}{port}{parts.path}"


# --------------------------------------------------------------------------- redaction


def redact(text: str, secret: str | None) -> str:
    return text.replace(secret, REDACTED) if secret and isinstance(text, str) else text


DETAIL_LIMIT = 500
_BEARER = re.compile(r"(?i)\bbearer\s+[^\s\"',;]+")
_META_TOKEN = re.compile(r"\bEAA[A-Za-z0-9]{20,}")


def sanitize_detail(text: Any, secret: str | None = None) -> str | None:
    """Server text made safe to store and print: the exact token and any `Bearer <...>` value removed,
    whitespace collapsed to one line, at most DETAIL_LIMIT characters."""
    if text is None:
        return None
    text = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False, default=str)
    if secret:
        text = text.replace(secret, REDACTED)
    text = _BEARER.sub(f"Bearer {REDACTED}", text)
    text = _META_TOKEN.sub(REDACTED, text)
    text = " ".join(text.split())
    if not text:
        return None
    return text if len(text) <= DETAIL_LIMIT else text[:DETAIL_LIMIT - 1] + "…"


def scrub_value(value: Any, secret: str | None) -> Any:
    """The token removed from every string inside a JSON-like value (structure and length otherwise kept)."""
    if not secret:
        return value
    if isinstance(value, str):
        return value.replace(secret, REDACTED)
    if isinstance(value, list):
        return [scrub_value(v, secret) for v in value]
    if isinstance(value, dict):
        return {k: scrub_value(v, secret) for k, v in value.items()}
    return value


@contextmanager
def scrub_logs(secret: str) -> Iterator[None]:
    """Remove `secret` from every log record created while active (all loggers)."""
    previous = logging.getLogRecordFactory()

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = previous(*args, **kwargs)
        try:
            message = record.getMessage()
        except Exception:  # a broken format string is not ours to fix
            return record
        if secret in message:
            record.msg, record.args = redact(message, secret), None
        if record.exc_info and record.exc_info[1] is not None and secret in str(record.exc_info[1]):
            record.exc_info, record.exc_text = None, None
            record.msg = f"{record.msg} [exception details removed: contained a secret]"
        return record

    logging.setLogRecordFactory(factory)
    try:
        yield
    finally:
        logging.setLogRecordFactory(previous)


# --------------------------------------------------------------------------- failure classes


def _leaves(exc: BaseException) -> list[BaseException]:
    if isinstance(exc, BaseExceptionGroup):
        return [leaf for sub in exc.exceptions for leaf in _leaves(sub)]
    return [exc]


def _http_detail(e: httpx.HTTPStatusError) -> str:
    response = e.response
    parts = [f"HTTP {response.status_code} {response.reason_phrase}".strip()]
    challenge = response.headers.get("www-authenticate")
    if challenge:
        parts.append(f"www-authenticate: {challenge}")
    try:
        body = response.text  # read by the client's response hook for error statuses
    except httpx.ResponseNotRead:
        body = ""
    if body.strip():
        parts.append(f"body: {body}")
    return "; ".join(parts)


def _mcp_detail(e: McpError) -> str:
    error = e.error
    text = f"MCP error {error.code}: {error.message}"
    if error.data not in (None, "", {}, []):
        text += f" data: {json.dumps(error.data, ensure_ascii=False, default=str)}"
    return text


def classify(exc: BaseException) -> DownstreamFailure:
    """Failure class plus raw detail (not yet sanitised; HttpDownstream._run sanitises it)."""
    leaves = _leaves(exc)
    for e in leaves:
        if isinstance(e, DownstreamFailure):
            return e
    for e in leaves:
        if isinstance(e, httpx.HTTPStatusError):
            code = e.response.status_code
            cls = "unauthorized" if code == 401 else "forbidden" if code == 403 else "http_status"
            return DownstreamFailure(cls, code, _http_detail(e))
    for e in leaves:
        if isinstance(e, httpx.ConnectTimeout):
            return DownstreamFailure("timeout", detail=f"{type(e).__name__}: {e}")
        if isinstance(e, (httpx.ConnectError, httpx.RemoteProtocolError, ConnectionError)):
            return DownstreamFailure("unreachable", detail=f"{type(e).__name__}: {e}")
    for e in leaves:
        if isinstance(e, (httpx.TimeoutException, TimeoutError)):
            return DownstreamFailure("timeout", detail=f"{type(e).__name__}: no answer within the timeout")
    for e in leaves:
        if isinstance(e, (_MalformedMessage, ValidationError, json.JSONDecodeError)):
            return DownstreamFailure("malformed", detail=f"{type(e).__name__}: {e}")
        if isinstance(e, McpError):
            return DownstreamFailure("protocol_error", detail=_mcp_detail(e))
    e = leaves[0] if leaves else exc
    return DownstreamFailure("error", detail=f"{type(e).__name__}: {e}")


# --------------------------------------------------------------------------- downstream


def _transport_error_recorder(seen: list[str]) -> Callable[[Any], Awaitable[None]]:
    # The SDK hands transport-level problems (unparseable JSON, unexpected content type) to the
    # session's message handler as exceptions. Record them so the failure is classed "malformed",
    # and raise so the session gives up at once instead of waiting for a timeout.
    async def handler(message: Any) -> None:
        if isinstance(message, Exception):
            seen.append(f"{type(message).__name__}: {message}")
            raise _MalformedMessage(type(message).__name__)
    return handler


async def _read_error_body(response: httpx.Response) -> None:
    # Read (bounded by the server) the body of an error response before the SDK raises on its status, so
    # the failure can say what the server said.
    if response.status_code >= 400:
        await response.aread()


def _sealed(value: str) -> Callable[[], str]:
    def reveal() -> str:
        return value
    return reveal


class HttpDownstream:
    """Downstream over MCP streamable HTTP. Implements the proxy's Downstream protocol."""

    classifies_failures = True  # the proxy records failure classes and details from this downstream

    def __init__(self, url: str, token: str, *, timeout: float = DEFAULT_TIMEOUT,
                 transport: httpx.AsyncBaseTransport | None = None,
                 session_class: type[ClientSession] = ClientSession):
        self.url = check_url(url)
        if not isinstance(token, str) or not token.strip() or any(c in token for c in "\r\n"):
            raise DownstreamConfigError(f"the {TOKEN_ENV} value is empty or not a single line")
        self._secret = _sealed(token.strip())  # a closure, so vars()/__dict__ dumps never show the value
        self.timeout = float(timeout)
        self._transport = transport  # tests and demos only: e.g. a header-stripping transport
        self._session_class = session_class
        self.server_info: types.Implementation | None = None  # from the last initialize; reporting only

    def __repr__(self) -> str:
        return f"HttpDownstream({public_url(self.url)!r}, token={REDACTED})"

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(headers={"Authorization": f"Bearer {self._secret()}"},
                                 timeout=httpx.Timeout(self.timeout), follow_redirects=False,
                                 transport=self._transport, event_hooks={"response": [_read_error_body]})

    @asynccontextmanager
    async def _session(self, seen: list[str]) -> AsyncIterator[ClientSession]:
        async with self._client() as client:
            async with streamable_http_client(self.url, http_client=client) as (read, write, _session_id):
                async with self._session_class(read, write, message_handler=_transport_error_recorder(seen),
                                               client_info=CLIENT_INFO) as session:
                    self.server_info = (await session.initialize()).serverInfo
                    yield session

    async def _run(self, op: Callable[[ClientSession], Awaitable[T]]) -> T:
        seen: list[str] = []
        with scrub_logs(self._secret()):
            try:
                with anyio.fail_after(self.timeout):
                    async with self._session(seen) as session:
                        return await op(session)
            except anyio.get_cancelled_exc_class():
                raise
            except (Exception, BaseExceptionGroup) as exc:
                guard = next((e for e in _leaves(exc) if isinstance(e, DiscoveryViolation)), None)
                if guard is not None:
                    raise guard from None
                failure = classify(exc)
                if seen and failure.failure_class in ("protocol_error", "error"):
                    failure = DownstreamFailure("malformed", detail=seen[0])
                failure.detail = sanitize_detail(failure.detail, self._secret())
                raise failure from None

    async def list_tools(self) -> list[types.Tool]:
        async def op(session: ClientSession) -> list[types.Tool]:
            tools: list[types.Tool] = []
            cursor: str | None = None
            while True:
                page = await session.list_tools(cursor)
                tools.extend(page.tools)
                cursor = page.nextCursor
                if not cursor:
                    return tools
        return await self._run(op)

    async def call(self, name: str, arguments: Any):
        from .proxy import SessionDownstream

        from .proxy import describe_result

        async def op(session: ClientSession):
            return await SessionDownstream(session).call(name, arguments)
        res = await self._run(op)
        # The server may echo anything back, the token included: scrub before the proxy sees the result.
        secret = self._secret()
        res.text = redact(res.text, secret)
        res.structured = scrub_value(res.structured, secret)
        res.detail = sanitize_detail(describe_result(res), secret)
        return res


def http_downstream_from_env(url: str, *, timeout: float = DEFAULT_TIMEOUT, env: dict[str, str] | None = None,
                             session_class: type[ClientSession] = ClientSession) -> HttpDownstream:
    """Validate the URL and read the token; raises DownstreamConfigError before anything connects."""
    url = check_url(url)
    return HttpDownstream(url, token_from_env(env), timeout=timeout, session_class=session_class)


@asynccontextmanager
async def open_http_downstream(downstream: HttpDownstream) -> AsyncIterator[HttpDownstream]:
    """Context-manager form, to match the stdio downstream. Opens nothing: sessions are per operation."""
    yield downstream
