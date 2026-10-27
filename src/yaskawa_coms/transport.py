"""TCP and UDP socket transports shared by every protocol client.

Both expose the same small interface (``open``, ``close``, ``send``,
``recv_exact``, ``discard_input``) so protocol code doesn't care which one it
runs on. For UDP, ``recv_exact`` serves bytes out of the last datagram.
"""
from __future__ import annotations

import select
import socket
from typing import Optional

from .exceptions import CommunicationError, ResponseTimeout


class Transport:
    """Base class. ``source_port`` binds a fixed local port before connecting.

    Yaskawa connection settings name both the controller's port ("Local Port")
    and the PC's port ("Node Port"). If the controller is configured for a
    specific node port (rather than 0 / "unpassive open"), set ``source_port``
    to match or the controller will refuse the connection.
    """

    kind = "?"

    def __init__(
        self,
        host: str,
        port: int,
        timeout: float = 2.0,
        source_port: Optional[int] = None,
        source_ip: str = "",
    ):
        if not 0 < int(port) <= 0xFFFF:
            raise ValueError(f"invalid port {port}")
        self.host = host
        self.port = int(port)
        self.timeout = timeout
        self.source_port = source_port
        self.source_ip = source_ip
        self.sock: Optional[socket.socket] = None

    # -- lifecycle -------------------------------------------------------
    @property
    def is_open(self) -> bool:
        return self.sock is not None

    def open(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def _bind(self, sock: socket.socket) -> None:
        if self.source_port is not None or self.source_ip:
            # Allow quick reconnects on the same fixed port (TIME_WAIT).
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((self.source_ip, self.source_port or 0))

    def _require(self) -> socket.socket:
        if self.sock is None:
            raise CommunicationError("not connected")
        return self.sock

    # -- I/O -------------------------------------------------------------
    def send(self, data: bytes) -> None:
        raise NotImplementedError

    def recv_exact(self, n: int) -> bytes:
        raise NotImplementedError

    def recv_some(self, max_bytes: int = 65535) -> bytes:
        """Return whatever arrives next (at least 1 byte)."""
        raise NotImplementedError

    def discard_input(self) -> None:
        """Drop any unread bytes (e.g. a late reply to a timed-out request)."""

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.host}:{self.port}>"


class TcpTransport(Transport):
    kind = "tcp"

    def open(self) -> None:
        self.close()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self._bind(sock)
            sock.settimeout(self.timeout)
            sock.connect((self.host, self.port))
        except socket.timeout:
            sock.close()
            raise ResponseTimeout(f"timed out connecting to {self.host}:{self.port}") from None
        except OSError as exc:
            sock.close()
            raise CommunicationError(f"cannot connect to {self.host}:{self.port}: {exc}") from exc
        self.sock = sock

    def send(self, data: bytes) -> None:
        try:
            self._require().sendall(data)
        except socket.timeout:
            raise ResponseTimeout("timed out sending") from None
        except OSError as exc:
            raise CommunicationError(f"send failed: {exc}") from exc

    def recv_exact(self, n: int) -> bytes:
        sock = self._require()
        buf = bytearray()
        while len(buf) < n:
            try:
                chunk = sock.recv(n - len(buf))
            except socket.timeout:
                raise ResponseTimeout(
                    f"no response from {self.host}:{self.port} within {self.timeout}s"
                ) from None
            except OSError as exc:
                raise CommunicationError(f"receive failed: {exc}") from exc
            if not chunk:
                raise CommunicationError("controller closed the connection")
            buf += chunk
        return bytes(buf)

    def recv_some(self, max_bytes: int = 65535) -> bytes:
        sock = self._require()
        try:
            chunk = sock.recv(max_bytes)
        except socket.timeout:
            raise ResponseTimeout(f"no data within {self.timeout}s") from None
        except OSError as exc:
            raise CommunicationError(f"receive failed: {exc}") from exc
        if not chunk:
            raise CommunicationError("controller closed the connection")
        return chunk

    def discard_input(self) -> None:
        sock = self.sock
        if sock is None:
            return
        try:
            while select.select([sock], [], [], 0)[0]:
                if not sock.recv(65535):
                    break
        except OSError:
            pass


class UdpTransport(Transport):
    kind = "udp"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._buffer = b""

    def open(self) -> None:
        self.close()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self._bind(sock)
            sock.settimeout(self.timeout)
            # connect() on UDP only fixes the peer: replies from other hosts
            # are filtered out by the OS.
            sock.connect((self.host, self.port))
        except OSError as exc:
            sock.close()
            raise CommunicationError(f"cannot open UDP socket to {self.host}:{self.port}: {exc}") from exc
        self.sock = sock
        self._buffer = b""

    def send(self, data: bytes) -> None:
        self._buffer = b""
        try:
            self._require().send(data)
        except OSError as exc:
            raise CommunicationError(f"send failed: {exc}") from exc

    def _datagram(self) -> bytes:
        try:
            data = self._require().recv(65535)
        except socket.timeout:
            raise ResponseTimeout(
                f"no response from {self.host}:{self.port} within {self.timeout}s"
            ) from None
        except OSError as exc:
            # On Windows an ICMP "port unreachable" surfaces here.
            raise CommunicationError(f"receive failed: {exc}") from exc
        return data

    def recv_exact(self, n: int) -> bytes:
        while len(self._buffer) < n:
            if self._buffer:
                raise CommunicationError(
                    f"UDP datagram too short: wanted {n} bytes, got {len(self._buffer)}"
                )
            self._buffer = self._datagram()
        out, self._buffer = self._buffer[:n], self._buffer[n:]
        return out

    def recv_some(self, max_bytes: int = 65535) -> bytes:
        if self._buffer:
            out, self._buffer = self._buffer[:max_bytes], self._buffer[max_bytes:]
            return out
        return self._datagram()[:max_bytes]

    def discard_input(self) -> None:
        self._buffer = b""
        sock = self.sock
        if sock is None:
            return
        try:
            while select.select([sock], [], [], 0)[0]:
                sock.recv(65535)
        except OSError:
            pass


def make_transport(
    transport: str,
    host: str,
    port: int,
    timeout: float = 2.0,
    source_port: Optional[int] = None,
    source_ip: str = "",
) -> Transport:
    kinds = {"tcp": TcpTransport, "udp": UdpTransport}
    try:
        cls = kinds[transport.lower()]
    except KeyError:
        raise ValueError(f"transport must be 'tcp' or 'udp', not {transport!r}") from None
    return cls(host, port, timeout=timeout, source_port=source_port, source_ip=source_ip)
