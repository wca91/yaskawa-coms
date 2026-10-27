"""A small in-process controller simulator for tests and demos.

It answers Extended MEMOBUS or Modbus/TCP requests from a shared register
memory (MW/IW, with MB/IB as their bits). It follows this library's reading
of the manual, so it proves the client and the frame code agree with each
other - the byte-for-byte checks against the manual's examples live in the
test suite, and a real controller is the final word.

Example::

    from yaskawa_coms import MemobusClient
    from yaskawa_coms.simulator import MemobusSimulator

    with MemobusSimulator() as sim:
        sim.memory.set("MF21000", 8.68)
        with MemobusClient("127.0.0.1", sim.port) as plc:
            print(plc.read("MF21000"))
"""
from __future__ import annotations

import socket
import struct
import threading
from typing import Callable, List, Optional, Tuple

from . import memobus as mb
from . import modbus as mt
from .address import AddressLike, parse_address
from .datatypes import (
    bytes_to_words_be,
    bytes_to_words_le,
    float_to_words,
    int32_to_words,
    pack_bits,
    to_signed16,
    to_unsigned16,
    unpack_bits,
    words_to_bytes_be,
    words_to_bytes_le,
    words_to_float,
    words_to_int32,
)


class _Fault(Exception):
    def __init__(self, code: int):
        self.code = code


class RegisterMemory:
    """65536 MW words and 65536 IW words. Thread-safe enough for tests."""

    def __init__(self) -> None:
        self.mw: List[int] = [0] * 0x10000
        self.iw: List[int] = [0] * 0x10000
        self.lock = threading.Lock()

    def _table(self, area: str) -> List[int]:
        return self.mw if area == "M" else self.iw

    # bit helpers (coil n = word n//16, bit n%16)
    def get_bit(self, table: List[int], n: int) -> bool:
        return bool(table[n // 16] & (1 << (n % 16)))

    def set_bit(self, table: List[int], n: int, on: bool) -> None:
        if on:
            table[n // 16] |= 1 << (n % 16)
        else:
            table[n // 16] &= ~(1 << (n % 16)) & 0xFFFF

    # typed access for test setup
    def set(self, address: AddressLike, value) -> None:
        """Set a value by address, e.g. ``set("MF21000", 8.68)``. Unlike the
        network API this can also set I registers."""
        a = parse_address(address)
        t = self._table(a.area)
        with self.lock:
            if a.kind == "B":
                self.set_bit(t, a.coil, bool(value))
            elif a.kind == "W":
                t[a.word] = to_unsigned16(int(value))
            elif a.kind == "L":
                t[a.word], t[a.word + 1] = int32_to_words(int(value))
            else:
                t[a.word], t[a.word + 1] = float_to_words(float(value))

    def get(self, address: AddressLike):
        a = parse_address(address)
        t = self._table(a.area)
        with self.lock:
            if a.kind == "B":
                return self.get_bit(t, a.coil)
            if a.kind == "W":
                return to_signed16(t[a.word])
            lo, hi = t[a.word], t[a.word + 1]
        return words_to_int32(lo, hi) if a.kind == "L" else words_to_float(lo, hi)


def _check(start: int, count: int, limit: int, top: int = 0xFFFF) -> None:
    if not 1 <= count <= limit:
        raise _Fault(0x03)
    if start < 0 or start + count - 1 > top:
        raise _Fault(0x02)


class _Server:
    """Threaded TCP or UDP server. Subclasses implement ``read_request`` (TCP
    framing) and ``handle`` (request bytes -> reply bytes or None)."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0, transport: str = "tcp",
                 memory: Optional[RegisterMemory] = None):
        self.host = host
        self.transport = transport.lower()
        self.memory = memory or RegisterMemory()
        self.requests = 0
        self._requested_port = port
        self._sock: Optional[socket.socket] = None
        self._threads: List[threading.Thread] = []
        self._conns: List[socket.socket] = []
        self._stop = threading.Event()

    @property
    def port(self) -> int:
        assert self._sock is not None, "simulator not started"
        return self._sock.getsockname()[1]

    def start(self):
        kind = socket.SOCK_STREAM if self.transport == "tcp" else socket.SOCK_DGRAM
        self._sock = socket.socket(socket.AF_INET, kind)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.host, self._requested_port))
        self._sock.settimeout(0.2)
        if self.transport == "tcp":
            self._sock.listen(8)
            target = self._accept_loop
        else:
            target = self._udp_loop
        t = threading.Thread(target=target, daemon=True)
        t.start()
        self._threads.append(t)
        return self

    def stop(self) -> None:
        self._stop.set()
        for c in list(self._conns):
            try:
                c.close()
            except OSError:
                pass
        if self._sock is not None:
            self._sock.close()
        for t in self._threads:
            t.join(timeout=2)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    def drop_connections(self) -> None:
        """Close every client connection (to test reconnects)."""
        for c in list(self._conns):
            try:
                c.shutdown(socket.SHUT_RDWR)
                c.close()
            except OSError:
                pass

    # -- loops -----------------------------------------------------------
    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            conn.settimeout(0.2)
            self._conns.append(conn)
            t = threading.Thread(target=self._conn_loop, args=(conn,), daemon=True)
            t.start()
            self._threads.append(t)

    def _recv_exact(self, conn: socket.socket, n: int) -> Optional[bytes]:
        buf = b""
        while len(buf) < n:
            if self._stop.is_set():
                return None
            try:
                chunk = conn.recv(n - len(buf))
            except socket.timeout:
                continue
            except OSError:
                return None
            if not chunk:
                return None
            buf += chunk
        return buf

    def _conn_loop(self, conn: socket.socket) -> None:
        try:
            while not self._stop.is_set():
                request = self.read_request(lambda n: self._recv_exact(conn, n))
                if request is None:
                    return
                reply = self._safe_handle(request)
                if reply:
                    conn.sendall(reply)
        except OSError:
            pass
        finally:
            if conn in self._conns:
                self._conns.remove(conn)
            conn.close()

    def _udp_loop(self) -> None:
        while not self._stop.is_set():
            try:
                data, peer = self._sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                return
            reply = self._safe_handle(data)
            if reply:
                self._sock.sendto(reply, peer)

    def _safe_handle(self, request: bytes) -> Optional[bytes]:
        self.requests += 1
        try:
            return self.handle(request)
        except Exception:  # never let a bad frame kill the server
            return None

    # -- to implement ----------------------------------------------------
    def read_request(self, recv: Callable[[int], Optional[bytes]]) -> Optional[bytes]:
        raise NotImplementedError

    def handle(self, request: bytes) -> Optional[bytes]:
        raise NotImplementedError


class MemobusSimulator(_Server):
    """Extended MEMOBUS slave.

    Args:
        module: sets the per-request size limits it enforces.
        mw_size: number of MW/IW words that exist (a tested controller had
            32768). Reads starting past the end give error 02, reads running
            past the end give error 03 - as the real controller does.
        write_range: ``(first, last)`` MW words that may be written (error 02
            outside), like the controller's write-range setting.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 0, transport: str = "tcp",
                 memory: Optional[RegisterMemory] = None, module: str = "218IF-01",
                 mw_size: int = 0x10000, write_range: Optional[Tuple[int, int]] = None):
        super().__init__(host, port, transport, memory)
        self.limits = mb.MODULE_LIMITS[module.upper()]
        self.mw_size = mw_size
        self.write_range = write_range

    def _check_read(self, start: int, count: int, limit: int) -> None:
        if not 1 <= count <= limit:
            raise _Fault(0x03)
        if start >= self.mw_size:
            raise _Fault(0x02)
        if start + count > self.mw_size:
            raise _Fault(0x03)

    def _check_write(self, start: int, count: int) -> None:
        if start + count > self.mw_size:
            raise _Fault(0x02)
        if self.write_range:
            lo, hi = self.write_range
            if start < lo or start + count - 1 > hi:
                raise _Fault(0x02)

    def read_request(self, recv):
        header = recv(mb.HEADER_LEN)
        if header is None:
            return None
        total = struct.unpack_from("<H", header, 6)[0]
        rest = recv(total - mb.HEADER_LEN) if total > mb.HEADER_LEN else b""
        return None if rest is None else header + rest

    def handle(self, frame: bytes) -> Optional[bytes]:
        if len(frame) < 17 or frame[0] != mb.CMD_REQUEST or frame[14] != mb.MFC:
            return None
        serial, sfc, cpu = frame[1], frame[15], frame[16]
        f = frame[17:]
        rcpu = (cpu << 4) & 0xF0  # reply swaps the CPU nibbles (Appendix F)
        try:
            body = self._execute(sfc, f)
            pdu = bytes((mb.MFC, sfc, rcpu)) + body
        except (_Fault, struct.error, IndexError) as exc:
            code = exc.code if isinstance(exc, _Fault) else 0x03
            pdu = bytes((mb.MFC, sfc | 0x80, rcpu, code))
        return mb.build_frame(serial, pdu, command=mb.CMD_RESPONSE)

    def _execute(self, sfc: int, f: bytes) -> bytes:
        m = self.memory
        lim = self.limits
        with m.lock:
            if sfc in (mb.EXT_READ_HOLDING, mb.EXT_READ_INPUT):
                _, start, count = struct.unpack_from("<BHH", f)
                self._check_read(start, count, lim["read"])
                t = m.mw if sfc == mb.EXT_READ_HOLDING else m.iw
                return struct.pack("<BH", 0, count) + words_to_bytes_le(t[start:start + count])
            if sfc == mb.EXT_WRITE_HOLDING:
                _, start, count = struct.unpack_from("<BHH", f)
                _check(start, count, lim["write"])
                self._check_write(start, count)
                words = bytes_to_words_le(f[5:5 + 2 * count])
                if len(words) != count:
                    raise _Fault(0x03)
                m.mw[start:start + count] = words
                return struct.pack("<BHH", 0, start, count)
            if sfc == mb.EXT_READ_SCATTERED:
                _, count = struct.unpack_from("<BH", f)
                _check(0, count, lim["scattered_read"])
                addrs = bytes_to_words_le(f[3:3 + 2 * count])
                return struct.pack("<BH", 0, count) + words_to_bytes_le(m.mw[a] for a in addrs)
            if sfc == mb.EXT_WRITE_SCATTERED:
                _, count = struct.unpack_from("<BH", f)
                _check(0, count, lim["scattered_write"])
                pairs = [struct.unpack_from("<HH", f, 3 + 4 * i) for i in range(count)]
                for a, _ in pairs:
                    self._check_write(a, 1)
                for a, v in pairs:
                    m.mw[a] = v
                return struct.pack("<BH", 0, count)
            if sfc in (mb.READ_HOLDING, mb.READ_INPUT):
                start, count = struct.unpack_from(">HH", f)
                self._check_read(start, count, 125)
                t = m.mw if sfc == mb.READ_HOLDING else m.iw
                return words_to_bytes_be(t[start:start + count])
            if sfc in (mb.READ_COILS, mb.READ_INPUT_RELAYS):
                start, count = struct.unpack_from(">HH", f)
                _check(start, count, 2000, 0xFFFF)
                t = m.mw if sfc == mb.READ_COILS else m.iw
                return pack_bits([m.get_bit(t, start + i) for i in range(count)])
            if sfc == mb.WRITE_COIL:
                number, state, _ = struct.unpack_from(">HBB", f)
                self._check_write(number // 16, 1)
                m.set_bit(m.mw, number, state == 0xFF)
                return f[:4]
            if sfc == mb.WRITE_COILS:
                start, count = struct.unpack_from(">HH", f)
                _check(start, count, 800, 0xFFFF)
                self._check_write(start // 16, (start + count - 1) // 16 - start // 16 + 1)
                for i, on in enumerate(unpack_bits(f[4:], count)):
                    m.set_bit(m.mw, start + i, on)
                return f[:4]
            if sfc == mb.WRITE_REGISTER:
                address, value = struct.unpack_from(">HH", f)
                self._check_write(address, 1)
                m.mw[address] = value
                return f[:4]
            if sfc == mb.WRITE_REGISTERS:
                start, count = struct.unpack_from(">HH", f)
                _check(start, count, 100)
                self._check_write(start, count)
                m.mw[start:start + count] = bytes_to_words_be(f[4:4 + 2 * count])
                return f[:4]
            if sfc == mb.LOOPBACK:
                return f[:4]
        raise _Fault(0x01)


class ModbusSimulator(_Server):
    """Modbus/TCP slave on the same register memory model."""

    def read_request(self, recv):
        header = recv(mt.MBAP_LEN)
        if header is None:
            return None
        length = struct.unpack_from(">H", header, 4)[0]
        rest = recv(length - 1) if length > 1 else b""
        return None if rest is None else header + rest

    def handle(self, adu: bytes) -> Optional[bytes]:
        if len(adu) < mt.MBAP_LEN + 1:
            return None
        tid, _, _, unit = struct.unpack_from(">HHHB", adu)
        fc = adu[mt.MBAP_LEN]
        f = adu[mt.MBAP_LEN + 1:]
        try:
            pdu = bytes((fc,)) + self._execute(fc, f)
        except (_Fault, struct.error, IndexError) as exc:
            pdu = bytes((fc | 0x80, exc.code if isinstance(exc, _Fault) else 0x03))
        return mt.build_adu(tid, unit, pdu)

    def _execute(self, fc: int, f: bytes) -> bytes:
        m = self.memory
        with m.lock:
            if fc in (mt.READ_HOLDING, mt.READ_INPUT):
                start, count = struct.unpack_from(">HH", f)
                _check(start, count, 125)
                t = m.mw if fc == mt.READ_HOLDING else m.iw
                data = words_to_bytes_be(t[start:start + count])
                return bytes((len(data),)) + data
            if fc in (mt.READ_COILS, mt.READ_DISCRETE_INPUTS):
                start, count = struct.unpack_from(">HH", f)
                _check(start, count, 2000, 0xFFFF)
                t = m.mw if fc == mt.READ_COILS else m.iw
                data = pack_bits([m.get_bit(t, start + i) for i in range(count)])
                return bytes((len(data),)) + data
            if fc == mt.WRITE_REGISTER:
                address, value = struct.unpack_from(">HH", f)
                m.mw[address] = value
                return f[:4]
            if fc == mt.WRITE_REGISTERS:
                start, count, nbytes = struct.unpack_from(">HHB", f)
                _check(start, count, 100)
                if nbytes != 2 * count:
                    raise _Fault(0x03)
                m.mw[start:start + count] = bytes_to_words_be(f[5:5 + nbytes])
                return f[:4]
            if fc == mt.WRITE_COIL:
                number, value = struct.unpack_from(">HH", f)
                if value not in (0xFF00, 0x0000):
                    raise _Fault(0x03)
                m.set_bit(m.mw, number, value == 0xFF00)
                return f[:4]
            if fc == mt.WRITE_COILS:
                start, count, nbytes = struct.unpack_from(">HHB", f)
                _check(start, count, 800, 0xFFFF)
                for i, on in enumerate(unpack_bits(f[5:5 + nbytes], count)):
                    m.set_bit(m.mw, start + i, on)
                return f[:4]
        raise _Fault(0x01)
