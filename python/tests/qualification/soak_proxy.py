"""A TCP proxy in front of one engine, for the soak's network faults.

It moves bytes and nothing else: what it carries is opaque to it. An end of stream on one side is a
half close on the other, and a reset is a reset, so the application sees what the engine did. On
top of that, three things the soak does to the network:

- ``stall``: nothing moves in either direction, and every socket stays open - packets that stop
  arriving, or an engine that stopped answering, as the application's socket sees it;
- ``refuse``: the listener is closed and every connection reset - an engine nobody can reach;
- ``reset()``: every connection is reset at once, and new ones are accepted as before.

A partition that outlives the connections through it is ``stall`` followed by ``reset()``.

The loop runs in a thread of its own, so the caller's thread can change the mode at any moment.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import struct
import threading
from collections.abc import Coroutine
from typing import Any, Literal

Mode = Literal["pass", "stall", "refuse"]
MODES: tuple[Mode, ...] = ("pass", "stall", "refuse")
CHUNK = 65536


def _abort(writer: asyncio.StreamWriter) -> None:
    """Close with a reset rather than an end of stream: SO_LINGER with a zero timeout."""
    sock = writer.get_extra_info("socket")
    if sock is not None:
        with contextlib.suppress(OSError):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    writer.transport.abort()


class _Connection:
    def __init__(
        self,
        client: tuple[asyncio.StreamReader, asyncio.StreamWriter],
        upstream: tuple[asyncio.StreamReader, asyncio.StreamWriter],
    ) -> None:
        self.client, self.upstream = client, upstream
        self.closed = False

    def open(self) -> bool:
        # A method rather than the attribute: another task can close it across an await.
        return not self.closed

    def reset(self) -> None:
        if self.closed:
            return
        self.closed = True
        _abort(self.client[1])
        _abort(self.upstream[1])


class Proxy:
    """One listener in front of ``upstream``; ``port`` is where the application connects."""

    def __init__(self, upstream_host: str, upstream_port: int, *, port: int = 0) -> None:
        self.upstream = (upstream_host, upstream_port)
        self.port = port
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._server: asyncio.Server | None = None
        self._flowing: asyncio.Event | None = None
        self._connections: set[_Connection] = set()
        self._handlers: set[asyncio.Task[None]] = set()
        self._mode: Mode = "pass"
        self.accepted = 0
        self.resets = 0

    # --- the caller's thread -------------------------------------------------------------------

    def start(self) -> int:
        self._thread.start()
        self._call(self._start())
        return self.port

    @property
    def mode(self) -> Mode:
        return self._mode

    def set_mode(self, mode: Mode) -> None:
        if mode not in MODES:
            raise ValueError(f"a proxy mode is one of {MODES}, not {mode!r}")
        self._call(self._set_mode(mode))

    def reset(self) -> int:
        """Reset every connection open now; the number reset."""
        return int(self._call(self._reset()))

    def open_connections(self) -> int:
        return int(self._call(self._count()))

    def close(self) -> None:
        if not self._thread.is_alive():
            return
        self._call(self._close())
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=10)
        self._loop.close()

    def _call(self, coroutine: Coroutine[Any, Any, Any]) -> Any:
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop).result(timeout=30)

    # --- the loop's thread ---------------------------------------------------------------------

    async def _start(self) -> None:
        self._flowing = asyncio.Event()
        self._flowing.set()
        await self._listen()

    async def _listen(self) -> None:
        self._server = await asyncio.start_server(
            self._accept, "127.0.0.1", self.port, reuse_address=True
        )
        self.port = self._server.sockets[0].getsockname()[1]

    async def _set_mode(self, mode: Mode) -> None:
        assert self._flowing is not None
        previous, self._mode = self._mode, mode
        if mode == "stall":
            self._flowing.clear()
        else:
            self._flowing.set()
        if mode == "refuse" and self._server is not None:
            self._server.close()
            self._server = None
            await self._reset()
        if previous == "refuse" and mode != "refuse":
            await self._listen()

    async def _reset(self) -> int:
        connections = list(self._connections)
        for connection in connections:
            connection.reset()
        self._connections.clear()
        self.resets += len(connections)
        return len(connections)

    async def _count(self) -> int:
        return len(self._connections)

    async def _close(self) -> None:
        assert self._flowing is not None
        if self._server is not None:
            self._server.close()
            self._server = None
        self._flowing.set()
        await self._reset()
        # Every connection's handler ends once its sockets are reset; wait for them, so none is
        # left pending when the loop stops.
        if self._handlers:
            await asyncio.wait(set(self._handlers), timeout=10)
        for handler in set(self._handlers):
            handler.cancel()

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        handler = asyncio.current_task()
        assert handler is not None
        self._handlers.add(handler)
        try:
            await self._serve(reader, writer)
        finally:
            self._handlers.discard(handler)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._mode == "refuse":
            _abort(writer)
            return
        try:
            upstream = await asyncio.open_connection(*self.upstream)
        except OSError:
            # The engine is down: the application sees what it would see from the engine's host.
            _abort(writer)
            return
        self.accepted += 1
        connection = _Connection((reader, writer), upstream)
        self._connections.add(connection)
        await asyncio.gather(
            self._pump(connection, reader, upstream[1]),
            self._pump(connection, upstream[0], writer),
        )
        self._connections.discard(connection)
        connection.closed = True
        for side in (writer, upstream[1]):
            if not side.transport.is_closing():
                side.close()

    async def _pump(
        self,
        connection: _Connection,
        source: asyncio.StreamReader,
        sink: asyncio.StreamWriter,
    ) -> None:
        assert self._flowing is not None
        while connection.open():
            await self._flowing.wait()
            try:
                data = await source.read(CHUNK)
            except (ConnectionResetError, BrokenPipeError):
                connection.reset()
                return
            except OSError:
                connection.reset()
                return
            if not connection.open():
                return
            if not data:
                # An end of stream is a half close on the other side, not a reset.
                if sink.can_write_eof() and not sink.transport.is_closing():
                    try:
                        sink.write_eof()
                    except OSError:
                        connection.reset()
                return
            await self._flowing.wait()
            if not connection.open():
                return
            try:
                sink.write(data)
                await sink.drain()
            except (ConnectionResetError, BrokenPipeError):
                connection.reset()
                return
            except OSError:
                connection.reset()
                return
