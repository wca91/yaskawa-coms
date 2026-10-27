# yaskawa-coms

Talk to Yaskawa machine controllers with 218 Ethernet modules (MP2000 /
MP3000 218IF, CP-series 218TXB / 218FXB / 218G) from Python. Three protocols with one style of API:

| Protocol | Class | Use it when |
|---|---|---|
| Extended MEMOBUS | `MemobusClient` | Normal case: read/write MW, MF, ML, MB, IW... |
| Modbus/TCP | `ModbusTcpClient` | The controller's connection is set to MODBUS/TCP (**check your module supports it first**, see [Modbus/TCP support](#modbustcp-check-your-module-first)) |
| Non-procedure | `NonProcedureClient` | The PLC program sends/receives raw MW data with MSG-SND / MSG-RCV |

Pure Python, no dependencies, Python 3.8+. TCP and UDP.

> Not affiliated with or endorsed by Yaskawa Electric Corporation.
> Test on a safe machine first: writing to a controller can move real equipment.

## Install

```bash
pip install yaskawa-coms
```

## Quick start

```python
from yaskawa_coms import MemobusClient

with MemobusClient("192.168.1.1", 10001) as plc:      # IP and the 218IF "Local Port"
    plc.read("MW00100")              # -> 1234          (signed 16-bit)
    plc.read("MW00100", 10)          # -> [1234, 0, ...]
    plc.read("MF21000")              # -> 8.6809        (float, MW21000 + MW21001)
    plc.read("ML00200")              # -> -100000       (32-bit int)
    plc.read("MB00100A")             # -> True          (bit A of MW00100)
    plc.read("IW0010")               # input register (hex number)

    plc.write("MW00100", 1234)
    plc.write("MW00100", [1, 2, 3])
    plc.write("MF21000", 8.68)
    plc.write("MB00100A", True)
```

Modbus/TCP and UDP work the same way:

```python
from yaskawa_coms import ModbusTcpClient, MemobusClient

ModbusTcpClient("192.168.1.1", 502, unit_id=0)
MemobusClient("192.168.1.1", 10002, transport="udp")
```

## Addresses

| Address | Meaning | Number base |
|---|---|---|
| `MW00100` | holding register (word) | decimal |
| `ML00100` | 32-bit int in MW00100 (low) + MW00101 (high) | decimal |
| `MF00100` | 32-bit float in MW00100 (low) + MW00101 (high) | decimal |
| `MB00100A` | bit A (10) of MW00100 | decimal word + 1 hex bit digit |
| `IW0010`, `IL`, `IF`, `IB0010F` | input registers (read only) | hexadecimal |

Short forms like `"MW100"` are fine. Values read from `MW` are signed
(-32768..32767, as MPE720 shows them). Use `read_words()` for raw unsigned
values.

## Controller setup (218IF connection)

In MPE720, open the 218IF module's connection settings and add a connection:

* **Local Port**: the port you pass to the client (no fixed default).
* **Node IP / Node Port**: your PC's IP and port, **or `0.0.0.0` / `0`
  ("unpassive open")** to accept any PC. With unpassive open, a new
  connection drops the old one, so use one client per connection.
* **Protocol type**: Extended MEMOBUS, MODBUS/TCP or None (non-procedure).
* **TCP/UDP** and **Code (BIN/ASCII)**: must match the client
  (`transport="udp"`; non-procedure: `code="ascii"`).

If the controller expects a fixed Node Port, give the client the same local
port: `MemobusClient(..., source_port=5000)`.

The 218IF handles at most 10 connections at the same time.

**The PLC also needs an MSG-RCV function in its ladder program**, otherwise
it never answers over Ethernet. See [docs/MSG_FUNCTIONS.md](https://github.com/wca91/yaskawa-coms/blob/main/docs/MSG_FUNCTIONS.md)
for a ready-to-copy ladder, the MSG-SND / MSG-RCV parameter tables, and how
to read their error codes.

**After changing the protocol** (the connection's protocol type and the
MSG-RCV Pro-Typ input), save and **restart the PLC**; the change does not
take effect until then. Note that PARAM02 in the MSG-RCV parameter list is
the connection number, not the protocol.

### Modbus/TCP: check your module first

Not every 218 Ethernet module can do Modbus/TCP. Setting a connection to
MODBUS/TCP on a module that doesn't support it can take the **whole module
off the network**: it no longer answers ping, MEMOBUS, or CP-717 / MPE720.
The module LEDs show RUN and RX lit but TX never lights, because the module
never finishes starting up.

Modbus/TCP is only supported by (CP-218 User's Manual, SIEP C870200 01E):

| Module | Modbus/TCP |
|---|---|
| 218TXB, version **S0220 or later**, in **expansion mode** (DIP switch X1 = ON) | Yes |
| 218FXB, version S0302 or later | Yes |
| 218G | Yes |
| 218TXB older than S0220 (e.g. **S0210**), or in basic mode (X1 = OFF) | **No** |
| 316/218IF, 317/218IF, 316H/218IFB, 218TX, 218FX | **No** |
| MP2000 218IF-01 / 218IF-02 | Yes (MP2000 manual SIEP C880700 04) |

The version is on the module's label (e.g. `87317-2184x-S020y` is an older
218TXB below S0220). Expansion mode also needs a recent enough CPU module;
see the CP-218 manual.

When the module does support it, the settings are:

* Connection: **TCP** + **BIN** (UDP and ASCII/RTU are not allowed).
* MSG-RCV **PRO-TYP**: **4** on CP-series (CP-218 manual); on MP2000 the
  manual says **1** (MEMOBUS) and the module converts automatically.
* Client: `ModbusTcpClient(ip, <Local Port>, unit_id=0)`. The port is the
  connection's Local Port, not automatically 502.

**If the module went silent after switching to Modbus/TCP:** set the INIT
DIP switch ON and power-cycle. The module then starts at **192.168.1.1**
(engineering port 10000/UDP) with its saved settings ignored. Connect CP-717
there (PC on 192.168.1.x), change the connection back to Extended MEMOBUS,
save to flash, set INIT OFF, and power-cycle.

## API

All register clients (`MemobusClient`, `ModbusTcpClient`) have:

| Method | |
|---|---|
| `read(address, count=None)` / `write(address, value_or_list)` | typed access by address string |
| `read_words(start, count)` / `write_words(start, values)` | raw MW (holding registers) |
| `read_input_words(start, count)` | raw IW (input registers) |
| `read_float(address)` / `write_float(address, value)` | MF helpers |
| `write_word_bit(word, bit, on)` | read-modify-write of one MW bit |
| `read_coils` / `write_coil` / `write_coils` / `read_input_relays` | MB/IB as coils (number = word * 16 + bit, up to MB04095F); `write_coil` is atomic |
| `connect()` / `close()` / `with ...:` | connection handling |

Large reads and writes are split into several requests automatically.

`MemobusClient` extras: `read_scattered([100, 205])`,
`write_scattered({100: 1, 205: 2})`, `loopback()`, the standard-code
versions `read_holding_std` / `write_registers_std`, and `request(pdu)` for
any function code.

Options: `cpu=1` (destination CPU, MP2000 = 1), `module="218IF-01"` or
`"218IF-02"` (size limits 508/507 vs 2044/2043 words), or set
`max_read_words` / `max_write_words` directly, `timeout=2.0`,
`retries=1` (reconnect and resend after a connection problem; error replies
from the controller are never retried).

### Errors

Everything raises a subclass of `YaskawaComError`:

* `CommunicationError` / `ResponseTimeout`: connection refused, closed, no reply
* `ProtocolError`: a reply that doesn't match the request
* `RemoteError`: the controller rejected the request; subclasses
  `IllegalFunction` (01), `IllegalAddress` (02), `IllegalDataCount` (03)
* `AddressError`: a bad address string, or writing an input register

### Non-procedure

```python
from yaskawa_coms import NonProcedureClient

with NonProcedureClient("192.168.1.1", 10003) as link:
    link.send_words([1, 2, 3])     # stored in MW by the PLC's MSG-RCV
    data = link.receive_words(10)  # 10 words sent by the PLC's MSG-SND
```

There is no reply in this protocol, and TCP may join or split messages that
are sent quickly one after another. Receive a fixed `count`, or leave at least
1 s between messages (the manual's advice).

## Simulator and CLI

Try everything without a controller:

```python
from yaskawa_coms import MemobusClient
from yaskawa_coms.simulator import MemobusSimulator

with MemobusSimulator() as sim:
    sim.memory.set("MF21000", 8.68)
    with MemobusClient("127.0.0.1", sim.port) as plc:
        print(plc.read("MF21000"))
```

```bash
yaskawa-coms simulate --port 10001
yaskawa-coms read  127.0.0.1:10001 MW00100 5
yaskawa-coms write 127.0.0.1:10001 MF21000 8.68
yaskawa-coms read  192.168.1.1:502 MW00100 --protocol modbus
```

## Status

Tested on a real MP-series controller over TCP (2026-10-27):

| Feature | State |
|---|---|
| Extended MEMOBUS 09H/0BH read/write, MF/ML/MW/MB typed API | tested on hardware |
| Standard codes 01H, 02H, 03H, 04H, 05H, 06H, 08H, 0FH, 10H | tested on hardware (big-endian fields, as in the manual's sample program) |
| Scattered read/write 0DH / 0EH | tested on hardware |
| Error replies (02 address, 03 count) | tested on hardware |
| Extended MEMOBUS over UDP | tested on hardware (read, write, bit, scattered, loopback, errors) |
| Modbus/TCP | standard protocol; tested against the simulator only (the test unit's 218TXB S0210 does not support it, see above) |
| Non-procedure, PC -> PLC (MSG-RCV, Pro-Typ 2, UDP, BIN) | tested on hardware (5 and 510 words, register offset PARAM12) |
| Non-procedure, PLC -> PC (MSG-SND) | built from the manual; tested against a socket echo only |

Things learned on the real controller:

* **Write range (MSG-RCV PARAM12 / PARAM13) limits writes only.** In
  MEMOBUS mode PARAM12 is the lowest and PARAM13 the highest MW address the
  PC may **write**. **Reads are never limited**: you can read any MW. A write
  outside the range gets error 02 (`IllegalAddress`) and nothing is changed.
  Tested: with PARAM12 = 5500 / PARAM13 = 10001, reading MW00000 works, but
  writing MW00000 (even its own value) is refused; MW05500-MW10001 can be
  written. To write lower registers, lower PARAM12 (e.g. 0); this is a ladder
  parameter, so no PLC restart is needed.
* **PARAM12 means something else per protocol.** MEMOBUS / Extended MEMOBUS:
  write range LO. Non-procedure: register offset (where received data is
  stored). Check it again after switching protocol.
* **Request size.** The unit tested took 1020 words per read and 1019 per write
  (neither the 218IF-01 nor 218IF-02 figure). Set
  `MemobusClient(..., max_read_words=1020, max_write_words=1019)` for full
  speed; the default 508/507 is safe everywhere. An oversized write gets no
  reply and the controller drops the connection.
* **Register area.** That unit had MW00000-MW32767. A read running past the end
  gets error 03, not 02.
* **Coils** are numbered word * 16 + bit with a 16-bit field, so `read_coils` /
  `write_coil` only reach MB04095F. `read("MB...")` / `write("MB...")` work on
  every word.
* **PLC-owned registers.** Some registers are overwritten by the PLC program
  every scan, so a write to them appears to "not stick". That's the program,
  not the network.

References: *Machine Controller MP2000 Series Communication Module User's
Manual* (SIEP C880700 04L), Appendix B (Extended MEMOBUS), D (non-procedure),
E/F (sample programs); *System Controller CP-218 User's Manual*
(SIEP C870200 01E) for CP-series 218 modules and Modbus/TCP support.

## Development

```bash
pip install -e ".[dev]"
pytest
```

## License

MIT
