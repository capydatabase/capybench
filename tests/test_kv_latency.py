"""Unit tests for the kv_latency scenario: the two protocol codecs and the measurement loop.

The network clients are exercised end to end only against a real store; everything that
decides what a reply means - and therefore whether a number is real - is tested here.
"""

from __future__ import annotations

import io

import pytest

from capybench.scenarios import kv_latency
from capybench.scenarios.kv_latency import (
    KVError,
    RestClient,
    encode_command,
    measure_fresh_connects,
    measure_warm,
    parse_rest_reply,
    read_reply,
)

# -- RESP ---------------------------------------------------------------------------------


def test_encode_command_is_an_array_of_bulk_strings() -> None:
    expected = b"*3\r\n$3\r\nSET\r\n$1\r\nk\r\n$3\r\nv\xc3\xa9\r\n"  # length in bytes
    assert encode_command(("SET", "k", "vé")) == expected


@pytest.mark.parametrize(
    ("wire", "value"),
    [
        (b"+PONG\r\n", "PONG"),
        (b":42\r\n", 42),
        (b"$5\r\nhello\r\n", "hello"),
        (b"$0\r\n\r\n", ""),
        (b"$-1\r\n", None),  # GET of a missing key
        (b"_\r\n", None),  # RESP3 null
        (b"*2\r\n$1\r\na\r\n:1\r\n", ["a", 1]),
        (b"*-1\r\n", None),
    ],
)
def test_read_reply_decodes_every_reply_type(wire: bytes, value: object) -> None:
    assert read_reply(io.BytesIO(wire)) == value


def test_read_reply_keeps_a_bulk_string_with_crlf_intact() -> None:
    assert read_reply(io.BytesIO(b"$4\r\na\r\nb\r\n")) == "a\r\nb"


def test_read_reply_raises_the_server_error() -> None:
    with pytest.raises(KVError, match="NOAUTH Authentication required"):
        read_reply(io.BytesIO(b"-NOAUTH Authentication required.\r\n"))


@pytest.mark.parametrize("wire", [b"", b"+PON", b"$5\r\nhel"])
def test_read_reply_rejects_a_truncated_reply(wire: bytes) -> None:
    with pytest.raises(KVError, match="closed mid-reply"):
        read_reply(io.BytesIO(wire))


def test_read_reply_rejects_an_unknown_type() -> None:
    with pytest.raises(KVError, match="unexpected RESP reply type"):
        read_reply(io.BytesIO(b"!oops\r\n"))


# -- REST ---------------------------------------------------------------------------------


def test_parse_rest_reply_returns_the_result() -> None:
    assert parse_rest_reply(200, b'{"result":"PONG"}') == "PONG"
    # A missing key must stay distinguishable from a missing result.
    assert parse_rest_reply(200, b'{"result":null}') is None


@pytest.mark.parametrize(
    ("status", "body", "match"),
    [
        (401, b'{"error":"unauthorized"}', "HTTP 401"),
        (200, b'{"error":"ERR wrong number of arguments"}', "wrong number"),
        (502, b"<html>bad gateway</html>", "not JSON"),
        (200, b"[1,2]", "not an object"),
        (200, b"{}", "no result"),
    ],
)
def test_parse_rest_reply_raises_on_failure(status: int, body: bytes, match: str) -> None:
    with pytest.raises(KVError, match=match):
        parse_rest_reply(status, body)


def test_rest_client_requires_https() -> None:
    import ssl

    with pytest.raises(KVError, match="https://"):
        RestClient("http://kv.example.com", "t", context=ssl.create_default_context())


# -- measurement --------------------------------------------------------------------------


class FakeClient:
    """Stands in for RestClient / RespClient: a tiny in-memory store with a call log."""

    def __init__(self, log: list[str], store: dict[str, str]) -> None:
        self.log = log
        self.store = store

    def connect(self) -> None:
        self.log.append("connect")

    def command(self, *args: str) -> object:
        self.log.append(" ".join(args[:2]))
        name = args[0]
        if name == "PING":
            return "PONG"
        if name == "SET":
            self.store[args[1]] = args[2]
            return "OK"
        if name == "GET":
            return self.store.get(args[1])
        if name == "DEL":
            return int(self.store.pop(args[1], None) is not None)
        raise AssertionError(f"unexpected command {args}")

    def close(self) -> None:
        self.log.append("close")


def test_measure_warm_times_each_command_and_cleans_up_its_key() -> None:
    log: list[str] = []
    store: dict[str, str] = {}
    results = measure_warm(lambda: FakeClient(log, store), key="capybench:k", repeats=7)
    assert {name: len(samples) for name, samples in results.items()} == {
        "ping": 7,
        "set": 7,
        "get": 7,
    }
    assert all(sample >= 0 for samples in results.values() for sample in samples)
    assert log.count("connect") == 1  # one warm connection for everything
    assert log[-2:] == ["DEL capybench:k", "close"]
    assert store == {}


def test_measure_warm_refuses_numbers_for_a_wrong_reply() -> None:
    class WrongGet(FakeClient):
        def command(self, *args: str) -> object:
            return "stale" if args[0] == "GET" else super().command(*args)

    log: list[str] = []
    store: dict[str, str] = {}
    with pytest.raises(KVError, match="get: expected"):
        measure_warm(lambda: WrongGet(log, store), key="k", repeats=3)
    assert log[-2:] == ["DEL k", "close"]  # cleaned up even on failure


def test_measure_fresh_connects_opens_a_connection_per_sample() -> None:
    log: list[str] = []
    latencies = measure_fresh_connects(lambda: FakeClient(log, {}), repeats=4)
    assert len(latencies) == 4
    assert log.count("connect") == 4
    assert log.count("close") == 4


def test_run_requires_the_token_before_touching_the_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from capybench.config import KVLatencyConfig, Suite, Target

    monkeypatch.delenv("CAPYBENCH_KV_TEST_TOKEN", raising=False)
    suite = Suite(
        results_dsn="host=localhost",
        targets={"t": Target(name="t", host="db.example.com", provider="capydb")},
        kv_latency=KVLatencyConfig(target="t", token_env="CAPYBENCH_KV_TEST_TOKEN"),
    )
    with pytest.raises(KVError, match="CAPYBENCH_KV_TEST_TOKEN"):
        # run=None: the token check fails before a Run is ever used.
        kv_latency.run(suite, run=None, log=lambda _: None)
