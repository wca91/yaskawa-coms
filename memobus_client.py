"""Yaskawa Extended MEMOBUS over TCP - thread-safe."""
import socket, struct, threading


class MemobusClient:
    def __init__(self, ip, port, cpu_no=0x10, timeout=2.0):
        self.ip, self.port, self.cpu_no, self.timeout = ip, port, cpu_no, timeout
        self.sock = None
        self._serial = 0
        self._lock = threading.Lock()

    def _next_serial(self):
        self._serial = (self._serial + 1) & 0xFF
        return self._serial

    def connect(self):
        if self.sock:
            try: self.sock.close()
            except: pass
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect((self.ip, self.port))
        self.sock = s

    def close(self):
        if self.sock:
            try: self.sock.close()
            except: pass
            self.sock = None

    def _header(self, total_len):
        return bytes([0x11, self._next_serial(), 0, 0, 0, 0,
                      total_len & 0xFF, (total_len >> 8) & 0xFF, 0, 0, 0, 0])

    def _wrap(self, memobus):
        mem_len = struct.pack('<H', len(memobus))
        payload = mem_len + bytes(memobus)
        total = 12 + len(payload)
        return self._header(total) + payload

    def _recv_n(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("PLC closed connection")
            buf += chunk
        return buf

    def _send_recv(self, packet):
        with self._lock:
            if not self.sock:
                self.connect()
            try:
                self.sock.sendall(packet)
                hdr = self._recv_n(12)
                total = hdr[6] | (hdr[7] << 8)
                body = self._recv_n(total - 12) if total > 12 else b""
                return hdr + body
            except (socket.timeout, ConnectionError, OSError):
                self.close()
                raise

    def read_holding(self, start, count):
        memobus = [0x20, 0x09, self.cpu_no, 0x00,
                   start & 0xFF, (start >> 8) & 0xFF,
                   count & 0xFF, (count >> 8) & 0xFF]
        resp = self._send_recv(self._wrap(memobus))
        if len(resp) < 20:
            raise RuntimeError(f"short response ({len(resp)} bytes)")
        if resp[15] & 0x80:
            err = resp[17] if len(resp) > 17 else 0
            raise RuntimeError(f"Read error code {err}")
        n = resp[18] | (resp[19] << 8)
        regs = []
        for i in range(n):
            idx = 20 + 2 * i
            if idx + 1 >= len(resp): break
            regs.append(resp[idx] | (resp[idx + 1] << 8))
        return regs

    def write_holding(self, start, values):
        body = bytearray([0x20, 0x0B, self.cpu_no, 0x00,
                          start & 0xFF, (start >> 8) & 0xFF,
                          len(values) & 0xFF, (len(values) >> 8) & 0xFF])
        for v in values:
            body.append(v & 0xFF)
            body.append((v >> 8) & 0xFF)
        resp = self._send_recv(self._wrap(body))
        if len(resp) < 18:
            raise RuntimeError("short write response")
        if resp[15] & 0x80:
            err = resp[17] if len(resp) > 17 else 0
            raise RuntimeError(f"Write error code {err}")
        return True

    def write_bit(self, address, bit, on):
        cur_regs = self.read_holding(address, 1)
        if not cur_regs:
            raise RuntimeError(f"read returned empty for MW{address}")
        cur = cur_regs[0]
        new = (cur | (1 << bit)) if on else (cur & ~(1 << bit) & 0xFFFF)
        if new != cur:
            self.write_holding(address, [new])
        return cur, new

    # ---- MF (32-bit float) helpers ----
    # An MF register spans 2 words: MF<n> = MW<n> (low word) + MW<n+1> (high word),
    # joined low-word-first, then decoded as IEEE-754. Verified against the PLC:
    # MW21000=0xE4F7, MW21001=0x410A  ->  MF21000 = 8.6809.
    def read_float(self, start):
        """Read one MF float at MW<start>/<start+1>."""
        regs = self.read_holding(start, 2)
        if len(regs) < 2:
            raise RuntimeError(f"short float read at MF{start}")
        return words_to_float(regs[0], regs[1])

    def read_floats(self, start, count):
        """Read `count` consecutive MF floats starting at MW<start> (2 words each)."""
        regs = self.read_holding(start, count * 2)
        return [words_to_float(regs[2 * i], regs[2 * i + 1])
                for i in range(count) if 2 * i + 1 < len(regs)]


def words_to_float(low, high):
    """Join two 16-bit MEMOBUS words (low word first) into an IEEE-754 float."""
    return struct.unpack('<f', struct.pack('<HH', low & 0xFFFF, high & 0xFFFF))[0]


def float_to_words(value):
    """Split a float into (low_word, high_word) for an MF write (low word first)."""
    low, high = struct.unpack('<HH', struct.pack('<f', value))
    return low, high
