"""Key-value round-trip latency, over both protocols a Redis-compatible store speaks.

A key-value store usually sits on the hot path in front of the database - a rate limiter,
a session lookup, a cache check - so what matters is one command's round trip as an app
sees it. Measured per protocol:

* **REST** - the Upstash-compatible HTTP API (``POST <rest_url>`` with a JSON array body
  and a bearer token), which is what serverless and edge runtimes use. One kept-alive
  HTTPS connection, so the warm numbers exclude the TLS handshake.
* **RESP** - the Redis wire protocol over TLS, routed by SNI, authenticated with
  ``AUTH <token>``; what long-lived processes (queues, pub/sub) use. One connection.

Variants, each recorded as min/avg/p50/p95/p99/max: ``<protocol>-ping`` (pure round trip),
``<protocol>-set`` and ``<protocol>-get`` (a small value under a run-scoped key), and
``<protocol>-fresh-connect`` (TLS handshake, plus ``AUTH`` for RESP, plus one ``PING`` -
the cost a client without connection reuse pays on every request).

Needs :attr:`~capybench.capabilities.Capability.KV`: the target's provider resolves where
the store answers; the access token comes from the environment variable named by
``token_env``. Both clients are standard-library only.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import ssl
import time
from collections.abc import Callable
from typing import BinaryIO
from urllib.parse import urlsplit

from .. import providers
from ..config import Suite
from ..providers import KVEndpoint
from ..stats import summarize_ms
from ..store import Run, Sample
from ._naming import run_token

SCENARIO = "kv_latency"

_WARMUP = 50
_TIMEOUT_S = 30.0
#: A small, fixed value - the size of a session token or a counter's JSON, not a blob.
_VALUE = "x" * 100


class KVError(RuntimeError):
    """The store answered with an error, or with something that is not a reply."""


# -- REST ---------------------------------------------------------------------------------


class RestClient:
    """A kept-alive HTTPS client for the Upstash-compatible REST API."""

    def __init__(self, rest_url: str, token: str, *, context: ssl.SSLContext) -> None:
        parts = urlsplit(rest_url)
        if parts.scheme != "https" or not parts.hostname:
            raise KVError(f"REST URL must be https://host[:port][/path], got {rest_url!r}")
        self._path = parts.path or "/"
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        self._conn = http.client.HTTPSConnection(
            parts.hostname, parts.port or 443, timeout=_TIMEOUT_S, context=context
        )

    def connect(self) -> None:
        self._conn.connect()

    def command(self, *args: str) -> object:
        self._conn.request("POST", self._path, body=json.dumps(list(args)), headers=self._headers)
        response = self._conn.getresponse()
        body = response.read()
        return parse_rest_reply(response.status, body)

    def close(self) -> None:
        self._conn.close()


def parse_rest_reply(status: int, body: bytes) -> object:
    """The ``result`` of a REST reply; raises on an HTTP or command error."""
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise KVError(f"REST reply is not JSON (HTTP {status}): {body[:200]!r}") from exc
    if not isinstance(payload, dict):
        raise KVError(f"REST reply is not an object (HTTP {status})")
    if status != 200 or "error" in payload:
        raise KVError(f"REST command failed (HTTP {status}): {payload.get('error')!r}")
    if "result" not in payload:
        raise KVError("REST reply carries no result")
    return payload["result"]


# -- RESP ---------------------------------------------------------------------------------


def encode_command(args: tuple[str, ...]) -> bytes:
    """A command as a RESP array of bulk strings - the only form servers accept."""
    out = [f"*{len(args)}\r\n".encode()]
    for arg in args:
        data = arg.encode()
        out.append(b"$%d\r\n%s\r\n" % (len(data), data))
    return b"".join(out)


def read_reply(stream: BinaryIO) -> object:
    """Read one RESP2 reply (plus RESP3 null). An error reply raises :class:`KVError`."""
    line = stream.readline()
    if not line.endswith(b"\r\n"):
        raise KVError("connection closed mid-reply")
    kind, rest = line[:1], line[1:-2]
    if kind == b"+":
        return rest.decode()
    if kind == b"-":
        raise KVError(rest.decode())
    if kind == b":":
        return int(rest)
    if kind == b"_":
        return None
    if kind == b"$":
        length = int(rest)
        if length < 0:
            return None
        data = stream.read(length + 2)
        if len(data) != length + 2:
            raise KVError("connection closed mid-reply")
        return data[:-2].decode()
    if kind == b"*":
        count = int(rest)
        return None if count < 0 else [read_reply(stream) for _ in range(count)]
    raise KVError(f"unexpected RESP reply type {kind!r}")


class RespClient:
    """One RESP connection over TLS. ``host`` is also the SNI name the store is routed by."""

    def __init__(self, host: str, port: int, token: str, *, context: ssl.SSLContext) -> None:
        self._host = host
        self._port = port
        self._token = token
        self._context = context
        self._sock: ssl.SSLSocket | None = None
        self._stream: BinaryIO | None = None

    def connect(self) -> None:
        raw = socket.create_connection((self._host, self._port), timeout=_TIMEOUT_S)
        raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._sock = self._context.wrap_socket(raw, server_hostname=self._host)
        self._stream = self._sock.makefile("rb")
        if self.command("AUTH", self._token) != "OK":
            raise KVError("AUTH was not acknowledged")

    def command(self, *args: str) -> object:
        if self._sock is None or self._stream is None:
            raise KVError("not connected")
        self._sock.sendall(encode_command(args))
        return read_reply(self._stream)

    def close(self) -> None:
        if self._stream is not None:
            self._stream.close()
        if self._sock is not None:
            self._sock.close()
        self._sock = self._stream = None


# -- scenario -----------------------------------------------------------------------------


Client = RestClient | RespClient


def run(suite: Suite, run: Run, *, log: Callable[[str], None]) -> None:
    cfg = suite.kv_latency
    if cfg is None:
        log("kv_latency: no [kv_latency] config; skipping")
        return

    token = os.environ.get(cfg.token_env, "")
    if not token:
        raise KVError(
            f"kv_latency: set {cfg.token_env} to the K/V store's access token "
            f"(returned when the store is created or its token rotated)"
        )
    target = suite.target(cfg.target)
    endpoint = providers.for_target(suite, target).kv_endpoint(target)
    key = f"capybench:kv_latency:{run_token(run.run_id)}"
    context = ssl.create_default_context()

    def rest() -> RestClient:
        return RestClient(endpoint.rest_url, token, context=context)

    def resp() -> RespClient:
        return RespClient(endpoint.redis_host, endpoint.redis_port, token, context=context)

    for protocol, factory in (("rest", rest), ("resp", resp)):
        log(f"kv_latency: {cfg.target} over {protocol.upper()} ({_describe(endpoint, protocol)})")
        warm = measure_warm(factory, key=f"{key}:{protocol}", repeats=cfg.repeats)
        fresh = measure_fresh_connects(factory, repeats=cfg.connect_repeats)
        for variant, latencies in {**warm, "fresh-connect": fresh}.items():
            summary = _record(run, suite, cfg.target, f"{protocol}-{variant}", protocol, latencies)
            log(
                f"kv_latency: {cfg.target} {protocol}-{variant} "
                f"p50={summary['p50_ms']:.2f}ms p95={summary['p95_ms']:.2f}ms "
                f"p99={summary['p99_ms']:.2f}ms (n={len(latencies)})"
            )


def measure_warm(
    factory: Callable[[], Client], *, key: str, repeats: int
) -> dict[str, list[float]]:
    """Time PING, SET and GET on one warm connection. Deletes ``key`` afterwards."""
    results: dict[str, list[float]] = {"ping": [], "set": [], "get": []}
    client = factory()
    client.connect()
    try:
        for _ in range(_WARMUP):  # not recorded
            client.command("PING")
        commands: dict[str, tuple[str, ...]] = {
            "ping": ("PING",),
            "set": ("SET", key, _VALUE),
            "get": ("GET", key),
        }
        for variant, args in commands.items():
            for _ in range(repeats):
                started = time.perf_counter()
                reply = client.command(*args)
                results[variant].append((time.perf_counter() - started) * 1000.0)
            _check(variant, reply)
    finally:
        try:
            client.command("DEL", key)
        finally:
            client.close()
    return results


def measure_fresh_connects(factory: Callable[[], Client], *, repeats: int) -> list[float]:
    """Time connect (TLS, plus AUTH for RESP) and one PING on a new connection each time."""
    latencies: list[float] = []
    for _ in range(repeats):
        client = factory()
        started = time.perf_counter()
        try:
            client.connect()
            client.command("PING")
            latencies.append((time.perf_counter() - started) * 1000.0)
        finally:
            client.close()
    return latencies


def _check(variant: str, reply: object) -> None:
    """A wrong reply means the numbers measured something other than the command."""
    expected = {"ping": "PONG", "set": "OK", "get": _VALUE}[variant]
    if reply != expected:
        raise KVError(f"{variant}: expected {expected!r}, got {reply!r}")


def _describe(endpoint: KVEndpoint, protocol: str) -> str:
    if protocol == "rest":
        return endpoint.rest_url
    return f"{endpoint.redis_host}:{endpoint.redis_port}"


def _record(
    run: Run,
    suite: Suite,
    target_name: str,
    variant: str,
    protocol: str,
    latencies: list[float],
) -> dict[str, float]:
    target = suite.target(target_name)
    summary = summarize_ms(latencies)
    for metric, value in summary.items():
        run.record(
            Sample(
                scenario=SCENARIO,
                target=target_name,
                target_tier_usd=target.tier_usd,
                region=target.region,
                concurrency=1,
                variant=variant,
                metric=metric,
                value=value,
                unit="ms",
                params={"n": len(latencies), "protocol": protocol},
            )
        )
    return summary
