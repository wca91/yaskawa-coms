"""End-to-end tests over real sockets against the built-in simulator."""
import socket
import threading

import pytest

from yaskawa_coms import (
    CommunicationError,
    IllegalAddress,
    MemobusClient,
    ModbusTcpClient,
    NonProcedureClient,
    ResponseTimeout,
)
from yaskawa_coms.address import AddressError
from yaskawa_coms.simulator import MemobusSimulator, ModbusSimulator


@pytest.fixture(params=["tcp", "udp"])
def memobus(request):
    with MemobusSimulator(transport=request.param) as sim:
        with MemobusClient("127.0.0.1", sim.port, transport=request.param, timeout=1) as plc:
            yield sim, plc


@pytest.fixture(params=["tcp", "udp"])
def modbus(request):
    with ModbusSimulator(transport=request.param) as sim:
        with ModbusTcpClient("127.0.0.1", sim.port, transport=request.param, timeout=1) as plc:
            yield sim, plc


@pytest.fixture(params=[("memobus", "tcp"), ("memobus", "udp"), ("modbus", "tcp"), ("modbus", "udp")],
                ids=lambda p: "-".join(p))
def any_client(request):
    protocol, transport = request.param
    sim_cls, client_cls = (
        (MemobusSimulator, MemobusClient) if protocol == "memobus" else (ModbusSimulator, ModbusTcpClient)
    )
    with sim_cls(transport=transport) as sim:
        with client_cls("127.0.0.1", sim.port, transport=transport, timeout=1) as plc:
            yield sim, plc


# --- typed API, same behaviour on both protocols -------------------------
def test_words(any_client):
    sim, plc = any_client
    plc.write("MW00100", [1, -2, 65535])
    assert plc.read("MW00100", 3) == [1, -2, -1]
    assert plc.read_words(100, 3) == [1, 0xFFFE, 0xFFFF]
    assert sim.memory.get("MW00101") == -2
    assert plc.read("MW00100") == 1


def test_float_and_long(any_client):
    sim, plc = any_client
    sim.memory.mw[21000:21002] = [0xE4F7, 0x410A]  # PLC-verified pair
    assert plc.read("MF21000") == pytest.approx(8.6809, abs=1e-4)
    plc.write("MF21002", [1.5, -2.25])
    assert plc.read("MF21002", 2) == [1.5, -2.25]
    plc.write("ML00200", -100000)
    assert plc.read("ML00200") == -100000
    assert plc.read_float(21000) == pytest.approx(8.6809, abs=1e-4)


def test_bits_via_word(any_client):
    sim, plc = any_client
    sim.memory.mw[100] = 0x00F0
    plc.write("MB00100A", True)
    assert sim.memory.mw[100] == 0x04F0
    assert plc.read("MB00100A") is True
    assert plc.read("MB001004", 4) == [True] * 4
    plc.write("MB001004", [False, False])
    assert sim.memory.mw[100] == 0x04C0
    # a bit run that crosses into the next word
    plc.write("MB00100F", [True, True])
    assert sim.memory.mw[100] & 0x8000 and sim.memory.mw[101] & 0x0001
    assert plc.write_word_bit(102, 3, True) == (0, 8)


def test_input_registers_read_only(any_client):
    sim, plc = any_client
    sim.memory.set("IW0010", 77)
    sim.memory.set("IB0011F", True)
    assert plc.read("IW0010") == 77
    assert plc.read("IB0011F") is True
    with pytest.raises(AddressError):
        plc.write("IW0010", 1)


def test_coils(any_client):
    sim, plc = any_client
    plc.write_coil(100 * 16 + 2, True)
    assert sim.memory.get("MB001002") is True
    plc.write_coils(1600, [True, False, True])
    assert plc.read_coils(1600, 3) == [True, False, True]
    sim.memory.set("IB00205", True)
    assert plc.read_input_relays(0x20 * 16, 6)[5] is True


def test_large_read_is_split(memobus):
    sim, plc = memobus
    sim.memory.mw[0:1500] = list(range(1500))
    before = sim.requests
    assert plc.read_words(0, 1500) == list(range(1500))
    assert sim.requests - before == 3  # 508 + 508 + 484
    plc.write_words(2000, list(range(1200)))
    assert sim.memory.mw[2000:3200] == list(range(1200))


def test_218if02_limits():
    with MemobusSimulator(module="218IF-02") as sim:
        with MemobusClient("127.0.0.1", sim.port, module="218IF-02") as plc:
            before = sim.requests
            plc.read_words(0, 2044)
            assert sim.requests - before == 1


def test_remote_error_is_raised_not_retried(memobus):
    sim, plc = memobus
    from yaskawa_coms import memobus as mb

    from yaskawa_coms import IllegalDataCount

    before = sim.requests
    with pytest.raises(IllegalDataCount):
        # 09H read running past the end of MW -> error 03, as the real PLC does
        plc.request(mb.pdu_ext_read(mb.EXT_READ_HOLDING, plc.cpu_no, 65530, 10))
    assert sim.requests - before == 1


def test_memobus_extras(memobus):
    sim, plc = memobus
    sim.memory.mw[5], sim.memory.mw[900] = 11, 22
    assert plc.read_scattered([900, 5]) == [22, 11]
    plc.write_scattered({7: 70, 1000: 100})
    assert (sim.memory.mw[7], sim.memory.mw[1000]) == (70, 100)
    plc.write_registers_std(300, [1, 2, 3])
    plc.write_register_std(303, 4)
    assert plc.read_holding_std(300, 4) == [1, 2, 3, 4]
    sim.memory.iw[3] = 9
    assert plc.read_input_std(3, 1) == [9]
    assert plc.loopback()


def test_serial_number_wraps(memobus):
    sim, plc = memobus
    for _ in range(300):
        plc.read_words(0, 1)


def test_reconnects_after_drop():
    with MemobusSimulator() as sim:
        with MemobusClient("127.0.0.1", sim.port, timeout=1) as plc:
            plc.write("MW1", 5)
            sim.drop_connections()
            assert plc.read("MW1") == 5  # retried on a new connection


def test_timeout_when_controller_is_silent():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    conns = []
    threading.Thread(target=lambda: conns.append(server.accept()), daemon=True).start()
    try:
        plc = MemobusClient("127.0.0.1", server.getsockname()[1], timeout=0.3, retries=0)
        with pytest.raises(ResponseTimeout):
            plc.read("MW0")
        plc.close()
    finally:
        server.close()


def test_connection_refused():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    with pytest.raises(CommunicationError):
        MemobusClient("127.0.0.1", port, timeout=0.5, retries=0).read("MW0")


def test_fixed_source_port():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    free_port = probe.getsockname()[1]
    probe.close()
    with MemobusSimulator() as sim:
        with MemobusClient("127.0.0.1", sim.port, source_port=free_port) as plc:
            plc.read("MW0")
            assert plc.transport.sock.getsockname()[1] == free_port


def test_thread_safety(memobus):
    sim, plc = memobus
    errors = []

    def worker(base):
        try:
            for i in range(30):
                plc.write_words(base, [i])
                assert plc.read_words(base, 1) == [i]
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n * 10,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors


# --- non-procedure ---------------------------------------------------------
class _EchoServer:
    """Stands in for a controller doing MSG-RCV then MSG-SND."""

    def __init__(self, transport):
        kind = socket.SOCK_STREAM if transport == "tcp" else socket.SOCK_DGRAM
        self.sock = socket.socket(socket.AF_INET, kind)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.received = []
        self.transport = transport
        if transport == "tcp":
            self.sock.listen(1)
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            if self.transport == "tcp":
                conn, _ = self.sock.accept()
                while True:
                    data = conn.recv(4096)
                    if not data:
                        return
                    self.received.append(data)
                    conn.sendall(data)
            else:
                while True:
                    data, peer = self.sock.recvfrom(4096)
                    self.received.append(data)
                    self.sock.sendto(data, peer)
        except OSError:
            pass

    def close(self):
        self.sock.close()


@pytest.mark.parametrize("transport", ["tcp", "udp"])
def test_nonprocedure_words(transport):
    server = _EchoServer(transport)
    try:
        with NonProcedureClient("127.0.0.1", server.port, transport=transport) as link:
            link.send_words([0x1234, 0x5678, -1])
            assert link.receive_words(3) == [0x1234, 0x5678, 0xFFFF]
        # words travel low byte first (Appendix D)
        assert server.received[0] == bytes.fromhex("34 12 78 56 FF FF")
    finally:
        server.close()


def test_nonprocedure_ascii():
    server = _EchoServer("tcp")
    try:
        with NonProcedureClient("127.0.0.1", server.port, code="ascii") as link:
            link.send_bytes(bytes.fromhex("12 34 9A"))
            assert link.receive_bytes(3) == bytes.fromhex("12 34 9A")
        assert server.received[0] == b"12349A"
    finally:
        server.close()


def test_nonprocedure_limits():
    link = NonProcedureClient("127.0.0.1", 1)
    with pytest.raises(ValueError):
        link.send_words([])
    with pytest.raises(ValueError):
        link.send_words([0] * 511)


def test_cli_read_write(capsys):
    from yaskawa_coms.cli import main

    with MemobusSimulator() as sim:
        target = f"127.0.0.1:{sim.port}"
        assert main(["write", target, "MF21000", "2.5"]) == 0
        assert main(["read", target, "MF21000", "2"]) == 0
        out = capsys.readouterr().out
        assert "MF21000\t2.5" in out and "MF21002\t0.0" in out
        assert main(["read", target, "MX1"]) == 1


# --- behaviour seen on the real controller (2026-10-27) --------------------
def test_limits_can_be_overridden():
    with MemobusSimulator(module="218IF-02") as sim:
        with MemobusClient("127.0.0.1", sim.port, max_read_words=1020, max_write_words=1019) as plc:
            before = sim.requests
            plc.read_words(0, 2040)
            assert sim.requests - before == 2
            before = sim.requests
            plc.write_words(0, [1] * 1019)
            assert sim.requests - before == 1
    with pytest.raises(ValueError):
        MemobusClient("127.0.0.1", 1, max_read_words=5000)


def test_write_outside_write_range_explains():
    from yaskawa_coms import IllegalAddress

    with MemobusSimulator(mw_size=32768, write_range=(0, 10001)) as sim:
        with MemobusClient("127.0.0.1", sim.port) as plc:
            plc.write("MW10001", 1)
            with pytest.raises(IllegalAddress, match="write range"):
                plc.write("MW30000", 1)
            with pytest.raises(IllegalAddress):
                plc.write_register_std(10002, 1)


def test_read_past_end_of_area_explains():
    from yaskawa_coms import IllegalDataCount

    with MemobusSimulator(mw_size=32768) as sim:
        with MemobusClient("127.0.0.1", sim.port) as plc:
            assert plc.read_words(32767, 1) == [0]
            with pytest.raises(IllegalAddress):
                plc.read_words(32768, 1)
            with pytest.raises(IllegalDataCount, match="past the end"):
                plc.read_words(32500, 508)


def test_coil_numbers_are_16_bit(any_client):
    sim, plc = any_client
    plc.write_coil(0xFFFF, True)                 # MB04095F, the last coil
    assert sim.memory.get("MB04095F") is True
    with pytest.raises(AddressError):
        plc.write_coil(0x10000, True)
    with pytest.raises(AddressError):
        plc.read_coils(0xFFF0, 17)
    plc.write("MB30000A", True)                  # word API still reaches every MB
    assert sim.memory.get("MB30000A") is True
