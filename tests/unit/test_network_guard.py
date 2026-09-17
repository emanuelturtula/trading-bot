"""Self-tests of the suite-wide network guard (spec 011, T14, AC32; decision D60).

The guard is installed for every test by ``tests/conftest.py``, so these tests run under it
exactly as the rest of the suite does. Outbound connections, name resolution and ``curl_cffi``
requests raise ``NetworkAccessError``, a ``BaseException`` that no ``except Exception`` can
hide, while loopback connections, ``socket.socketpair()`` and ``asyncio.run`` keep working.
Test addresses are documentation-only: ``example.invalid`` and ``192.0.2.1``.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import threading
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.fixtures.network_guard import LOOPBACK_HOSTS, NetworkAccessError, install_network_guard

_INVALID_HOST = "example" + ".invalid"
_DOCUMENTATION_ADDRESS = "192.0.2.1"


def test_the_error_is_a_base_exception_but_not_an_exception() -> None:
    assert issubclass(NetworkAccessError, BaseException)
    assert not issubclass(NetworkAccessError, Exception)


def test_loopback_hosts_are_exactly_the_spec_set() -> None:
    assert frozenset({"127.0.0.1", "::1", "localhost"}) == LOOPBACK_HOSTS


def test_the_conftest_installs_the_guard_for_every_test() -> None:
    assert socket.getaddrinfo.__module__ == "tests.fixtures.network_guard"
    assert socket.socket.connect.__module__ == "tests.fixtures.network_guard"
    assert socket.socket.connect_ex.__module__ == "tests.fixtures.network_guard"


def test_name_resolution_of_a_remote_host_raises() -> None:
    with pytest.raises(NetworkAccessError):
        socket.getaddrinfo(_INVALID_HOST, 443)


def test_name_resolution_of_bytes_hosts_is_checked_too() -> None:
    with pytest.raises(NetworkAccessError):
        socket.getaddrinfo(_INVALID_HOST.encode("ascii"), 443)


@pytest.mark.parametrize("host", [None, "127.0.0.1", "localhost", b"localhost"])
def test_name_resolution_of_loopback_and_none_hosts_is_allowed(host: str | bytes | None) -> None:
    assert socket.getaddrinfo(host, 80, type=socket.SOCK_STREAM)


def test_create_connection_to_a_remote_address_raises_before_any_packet() -> None:
    with pytest.raises(NetworkAccessError):
        socket.create_connection((_DOCUMENTATION_ADDRESS, 443), timeout=1)


def test_a_raw_connect_to_a_remote_address_raises() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        with pytest.raises(NetworkAccessError):
            sock.connect((_DOCUMENTATION_ADDRESS, 443))
        with pytest.raises(NetworkAccessError):
            sock.connect_ex((_DOCUMENTATION_ADDRESS, 443))


def test_urlopen_raises_the_guard_error_and_not_a_url_error() -> None:
    with pytest.raises(NetworkAccessError):
        urllib.request.urlopen("http://" + _INVALID_HOST + "/", timeout=1)


def test_a_loopback_server_accepts_a_loopback_connection() -> None:
    with socket.create_server(("127.0.0.1", 0)) as server:
        port = server.getsockname()[1]
        accepted: list[bytes] = []

        def serve() -> None:
            connection, _ = server.accept()
            with connection:
                accepted.append(connection.recv(1))

        worker = threading.Thread(target=serve)
        worker.start()
        with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
            client.sendall(b"x")
        worker.join(timeout=5)

    assert accepted == [b"x"]


def test_connect_ex_to_loopback_is_allowed() -> None:
    with socket.create_server(("127.0.0.1", 0)) as server:
        port = server.getsockname()[1]
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
            assert client.connect_ex(("127.0.0.1", port)) == 0
            connection, _ = server.accept()
            connection.close()


def test_asyncio_run_works_under_the_guard() -> None:
    async def answer() -> int:
        await asyncio.sleep(0)
        return 42

    assert asyncio.run(answer()) == 42


def test_a_socketpair_exchanges_a_byte_under_the_guard() -> None:
    left, right = socket.socketpair()
    with left, right:
        left.sendall(b"y")
        assert right.recv(1) == b"y"


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="AF_UNIX sockets are not available")
def test_af_unix_connections_are_allowed(tmp_path: Path) -> None:
    family = socket.AF_UNIX
    path = str(tmp_path / "guard.sock")
    try:
        server = socket.socket(family, socket.SOCK_STREAM)
        server.bind(path)
    except OSError:
        pytest.skip("AF_UNIX sockets cannot be bound on this platform")
    with server:
        server.listen(1)
        with socket.socket(family, socket.SOCK_STREAM) as client:
            client.connect(path)
            connection, _ = server.accept()
            connection.close()


def test_a_curl_cffi_request_raises_through_an_except_exception_block() -> None:
    from curl_cffi import requests as curl_requests

    def fetch() -> str:
        try:
            curl_requests.Session().get("https://" + _INVALID_HOST + "/")
        except Exception:
            return "swallowed"
        return "returned"

    with pytest.raises(NetworkAccessError):
        fetch()


def test_an_async_curl_cffi_request_raises() -> None:
    from curl_cffi import requests as curl_requests

    # An uninitialized session: its verbs go through the patched class-level ``request``, and no
    # event loop selector thread is started (the Windows proactor loop would need one).
    session = object.__new__(curl_requests.AsyncSession)

    with pytest.raises(NetworkAccessError):
        asyncio.run(session.get("https://" + _INVALID_HOST + "/"))


def test_installing_twice_on_a_fresh_monkeypatch_keeps_the_guard_working() -> None:
    patcher = pytest.MonkeyPatch()
    try:
        install_network_guard(patcher)
        with pytest.raises(NetworkAccessError):
            socket.getaddrinfo(_INVALID_HOST, 443)
    finally:
        patcher.undo()
    # The suite-wide guard installed for this test is still active after the undo.
    with pytest.raises(NetworkAccessError):
        socket.getaddrinfo(_INVALID_HOST, 443)


def test_the_guard_is_skipped_for_curl_cffi_when_it_cannot_be_imported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    patcher = pytest.MonkeyPatch()
    monkeypatch.setitem(sys.modules, "curl_cffi.requests", None)
    try:
        install_network_guard(patcher)
        with pytest.raises(NetworkAccessError):
            socket.getaddrinfo(_INVALID_HOST, 443)
    finally:
        patcher.undo()


@pytest.fixture
def restored_yfinance_state() -> Iterator[None]:
    """Restore yfinance's configuration and logger level; the cache location may move."""
    import yfinance

    logger = logging.getLogger("yfinance")
    saved = (
        yfinance.config.debug.hide_exceptions,
        yfinance.config.debug.logging,
        yfinance.config.network.retries,
        logger.level,
    )
    try:
        yield
    finally:
        yfinance.config.debug.hide_exceptions = saved[0]
        yfinance.config.debug.logging = saved[1]
        yfinance.config.network.retries = saved[2]
        logger.setLevel(saved[3])


@pytest.mark.usefixtures("restored_yfinance_state")
def test_the_real_client_without_a_fake_fails_loudly(tmp_path: Path) -> None:
    from trading_bot.data.yahoo.client import YFinanceClient
    from trading_bot.data.yahoo.history import HistoryQuery

    client = YFinanceClient(cache_dir=tmp_path / "yfinance")

    with pytest.raises(NetworkAccessError):
        client.history(HistoryQuery(symbol="SPY", interval="1d", period="1mo"))
