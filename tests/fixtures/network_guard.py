"""The suite-wide network guard (spec 011, Design 13.6; decision D60).

``install_network_guard`` makes outbound connections, remote name resolution and ``curl_cffi``
requests raise ``NetworkAccessError`` for the rest of a test. It blocks outbound connections
only: socket creation, ``bind``, ``listen``, ``accept`` and ``socket.socketpair()`` are never
patched, because every ``asyncio.run`` creates a self-pipe with ``socketpair()`` (a native call
on Linux, a loopback listener plus ``connect`` on Windows). ``tests/conftest.py`` installs it
for every test, so "tests without network" (CLAUDE.md) is enforced, not a convention.

Always import this module as ``tests.fixtures.network_guard``: a second import path would
create a second ``NetworkAccessError`` class that ``pytest.raises`` does not match.
"""

from __future__ import annotations

import socket
from collections.abc import Callable
from typing import Final, NoReturn, cast

import pytest

__all__ = ["LOOPBACK_HOSTS", "NetworkAccessError", "install_network_guard"]

LOOPBACK_HOSTS: Final = frozenset({"127.0.0.1", "::1", "localhost"})

type _AddressInfo = list[
    tuple[
        socket.AddressFamily,
        socket.SocketKind,
        int,
        str,
        tuple[str, int] | tuple[str, int, int, int] | tuple[int, bytes],
    ]
]
type _Connect = Callable[[socket.socket, object], None]
type _ConnectEx = Callable[[socket.socket, object], int]
type _GetAddressInfo = Callable[
    [bytes | str | None, bytes | str | int | None, int, int, int, int], _AddressInfo
]


class NetworkAccessError(BaseException):
    """A test tried to reach the network. Not an Exception, so no `except Exception` can hide it."""


def install_network_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """Block outbound network access until ``monkeypatch`` is undone.

    Patches ``socket.socket.connect`` and ``connect_ex`` (allowed for ``AF_UNIX`` sockets and
    loopback hosts), ``socket.getaddrinfo`` (allowed for a ``None`` host and loopback hosts) and
    ``curl_cffi``'s ``Session.request`` and ``AsyncSession.request`` (always blocked; skipped
    only when ``curl_cffi`` cannot be imported, in which case yfinance cannot use it either).
    """
    real_connect = cast(_Connect, socket.socket.connect)
    real_connect_ex = cast(_ConnectEx, socket.socket.connect_ex)
    real_getaddrinfo = cast(_GetAddressInfo, socket.getaddrinfo)

    def connect(self: socket.socket, address: object) -> None:
        _check_connect(self, address)
        real_connect(self, address)

    def connect_ex(self: socket.socket, address: object) -> int:
        _check_connect(self, address)
        return real_connect_ex(self, address)

    def getaddrinfo(
        host: bytes | str | None,
        port: bytes | str | int | None,
        family: int = 0,
        type: int = 0,  # the name of the socket.getaddrinfo parameter
        proto: int = 0,
        flags: int = 0,
    ) -> _AddressInfo:
        if host is not None and _host_text(host) not in LOOPBACK_HOSTS:
            raise NetworkAccessError(f"a test tried to resolve {_host_text(host)!r}")
        return real_getaddrinfo(host, port, family, type, proto, flags)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    try:
        from curl_cffi.requests import AsyncSession, Session
    except ImportError:
        return
    monkeypatch.setattr(Session, "request", _blocked_request)
    monkeypatch.setattr(AsyncSession, "request", _blocked_request)


def _check_connect(sock: socket.socket, address: object) -> None:
    unix_family = getattr(socket, "AF_UNIX", None)
    if unix_family is not None and sock.family == unix_family:
        return
    host = address[0] if isinstance(address, tuple) and address else None
    if isinstance(host, str | bytes) and _host_text(host) in LOOPBACK_HOSTS:
        return
    shown = _host_text(host) if isinstance(host, str | bytes) else type(address).__name__
    raise NetworkAccessError(f"a test tried to connect to {shown!r}")


def _host_text(host: str | bytes) -> str:
    if isinstance(host, bytes):
        return host.decode("ascii", errors="replace")
    return host


def _blocked_request(*args: object, **kwargs: object) -> NoReturn:
    raise NetworkAccessError("a test tried to send a curl_cffi request")
