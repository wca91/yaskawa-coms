# MSG-SND and MSG-RCV: PLC-side setup

How to set up the ladder program in an MP2000-series controller (218IF
Ethernet) so a PC running `yaskawa-coms` can talk to it.

Sources: *MP2000 Series Communication Module User's Manual* (SIEP C880700
04L), sections 5.7 (MSG-SND), 5.8 (MSG-RCV), 2.1 (automatic reception),
9.6.2 (ladder example) and Appendix A (offsets). Items marked **(tested)**
were confirmed on a real controller.

---

## 1. Which function do I need?

| What you want | PLC runs | Python side |
|---|---|---|
| PC reads/writes PLC registers (Extended MEMOBUS) | **MSG-RCV**, Pro-Typ = 1 | `MemobusClient` |
| PC reads/writes PLC registers (Modbus/TCP) | **MSG-RCV**, Pro-Typ = 1 | `ModbusTcpClient` |
| PC sends raw data into PLC registers | **MSG-RCV**, Pro-Typ = 2 or 3 | `NonProcedureClient.send_words()` |
| PLC sends raw data to the PC | **MSG-SND**, Pro-Typ = 2 or 3 | `NonProcedureClient.receive_words()` |
| PLC reads/writes another device (PLC is master) | **MSG-SND**, Pro-Typ = 1 | a slave on the other side |

> **On a 218IF-01 / 218IF-02 Ethernet port the PLC only answers while an
> MSG-RCV is running in the ladder.** Those ports do not support "automatic
> reception" (manual p.2-4); it exists only on the serial ports. No MSG-RCV
> means no reply, and the PC sees a timeout.

Protocol type 1 ("MEMOBUS") covers Extended MEMOBUS, MEMOBUS and Modbus/TCP.
The 218IF converts automatically according to the **connection settings** in
MPE720 (Module Configuration > 218IF > Transmission Parameters), so the
ladder is the same for all three.

### 1.1 Switching protocol (e.g. Extended MEMOBUS to non-procedure)

The protocol is **not** in the 17-word parameter list. Change it in two
places, then restart:

| Step | Where | Set |
|---|---|---|
| 1 | 218IF connection settings, row of your connection | Protocol type: Extended MEMOBUS / MODBUS/TCP / None (non-procedure); also TCP/UDP and BIN/ASCII |
| 2 | **Pro-Typ input** (4th input) of the MSG-RCV / MSG-SND block | 1 = MEMOBUS family, 2 = non-procedure words, 3 = non-procedure bytes |
| 3 | Save to flash and **restart (power-cycle) the PLC** | **Required (tested):** the new Pro-Typ / protocol did not take effect until the PLC was restarted |

Common mix-up: **PARAM02 (DW00002) is the connection number** (which row of
the 218IF connection table this MSG-RCV serves), not the protocol. Setting
PARAM02 = 2 makes the function serve connection 2 instead, and connection 1
stops answering.

Steps 1 and 2 must agree: Pro-Typ 1 with a "None" connection, or Pro-Typ 2
with an Extended MEMOBUS connection, does not work. Change the Python client
to match too (`MemobusClient` / `ModbusTcpClient` / `NonProcedureClient`).

---

## 2. Quick start: let `yaskawa-coms` read and write the PLC

### 2.1 Connection settings (MPE720, 218IF transmission parameters)

| Field | Set to |
|---|---|
| Local port | the port you pass to `MemobusClient(ip, port)`, e.g. 10002 |
| Node IP / node port | your PC, or `0.0.0.0` / `0` ("unpassive open", accepts any PC) |
| Connect type | TCP (or UDP, then use `transport="udp"`) |
| Protocol type | Extended MEMOBUS (or MODBUS/TCP) |
| Code | BIN |

Note the **connection number** (1-20) of that row; MSG-RCV needs it.

### 2.2 Ladder

This matches the manual's example (9.6.2) and the program on the tested PLC:

```
One scan at startup (low-speed drawing: SB000003, high-speed: SB000001)
IF SB000003 == true
    STORE 00001 -> DW00002     PARAM02 connection number
    STORE 00000 -> DW00008     PARAM08 coil offset
    STORE 00000 -> DW00009     PARAM09 input relay offset
    STORE 00000 -> DW00010     PARAM10 input register offset
    STORE 00000 -> DW00011     PARAM11 holding register offset
    STORE 00000 -> DW00012     PARAM12 write range LO
    STORE 10001 -> DW00013     PARAM13 write range HI
    STORE 00000 -> DW00014     PARAM14 system, must be cleared at startup
    STORE 00000 -> DW00024     (not a PARAM) normal-completion counter
    STORE 00000 -> DW00025     (not a PARAM) error counter
END_IF

SB000004 (always ON) --> MSG-RCV
    Execute  SB000004     always ON: re-arms after every message
    Abort    DB000202     keep OFF
    Dev-Typ  00016        218IF-02 / 218IFA/B/C  (00006 for 218IF-01)
    Pro-Typ  00001        MEMOBUS family
    Cir-No   00001        circuit number shown in MPE720
    Ch-No    00001        a buffer channel no other MSG function uses at the same time
    Param    DA00000      parameter list = DW00000..DW00016
    Busy     DB000210
    Complete DB000211     ON for 1 scan per message handled
    Error    DB000212     ON for 1 scan per failed message

IF DB000211 == true:  INC DW00024     count good messages
IF DB000212 == true:  INC DW00025     count errors
```

Then from Python:

```python
from yaskawa_coms import MemobusClient

with MemobusClient("192.168.1.1", 10002) as plc:
    plc.read("MW00000", 10)
    plc.write("MW00100", 1234)     # allowed: inside PARAM12..PARAM13
```

---

## 3. MSG-RCV reference (PLC answers the PC)

### 3.1 Inputs and outputs

| # | Name | Type | Meaning |
|---|---|---|---|
| 1 | Execute | bit | Receive. Hold ON (usually SB000004) so it keeps serving requests |
| 2 | Abort | bit | Abort; has priority over Execute. Keep OFF |
| 3 | Dev-Typ | int | 6 = 218IF (218IF-01), 16 = 218IFA/B/C (218IF-02), 5 = 217IF serial, 1 = MPLINK |
| 4 | Pro-Typ | int | 1 = MEMOBUS / Extended MEMOBUS / Modbus/TCP, 2 = non-procedure words, 3 = non-procedure bytes |
| 5 | Cir-No | int | Circuit number from MPE720 (1-8 for 218IF) |
| 6 | Ch-No | int | Transmission buffer channel 1-10 (218IF). Shared by all MSG-SND/MSG-RCV on that circuit; two functions running at the same time need different channels |
| 7 | Param | address | First register of the 17-word parameter list (e.g. DA00000) |
| 8 | Busy | bit | Processing |
| 9 | Complete | bit | ON for one scan after a message was handled |
| 10 | Error | bit | ON for one scan after an error |

### 3.2 Parameter list (Pro-Typ = 1)

With `Param = DA00000`, PARAMnn is DW000nn.

| PARAM | DW | Dir | Meaning |
|---|---|---|---|
| 00 | DW00000 | OUT | Processing result (see 5.1) |
| 01 | DW00001 | OUT | Status (see 5.2) |
| 02 | DW00002 | IN | **Connection number** 1-20, as in the 218IF settings |
| 03 | DW00003 | OUT | Option |
| 04 | DW00004 | OUT | Function code the PC sent (e.g. 0009H read, 000BH write) |
| 05 | DW00005 | OUT | Data address the PC asked for |
| 06 | DW00006 | OUT | Data size the PC asked for |
| 07 | DW00007 | OUT | Remote CPU number |
| 08 | DW00008 | IN | Coil offset (words) |
| 09 | DW00009 | IN | Input relay offset (words) |
| 10 | DW00010 | IN | Input register offset (words) |
| 11 | DW00011 | IN | Holding register offset (words) |
| 12 | DW00012 | IN | **Write range LO** (MW word address) |
| 13 | DW00013 | IN | **Write range HI** (MW word address) |
| 14 | DW00014 | SYS | System. Set to 0 in the first scan, then leave alone |
| 15-16 | DW00015-16 | SYS | System. Don't touch |

PARAM04-07 are handy when debugging: they show what the last request asked for.

### 3.3 Offsets (PARAM08-11)

The PLC adds the offset to the address the PC sends:

```
register used = address sent by the PC + offset
```

Example: PARAM11 = 1000 and the PC reads holding register 5, so the PLC
returns MW01005. Offsets cannot be negative. Which offset applies depends on
the function code:

| Offset | Applies to |
|---|---|
| PARAM08 coil | 01H, 05H, 0FH (MB) |
| PARAM09 input relay | 02H (IB) |
| PARAM10 input register | 04H, 0AH (IW) |
| PARAM11 holding register | 03H, 06H, 09H, 0BH, 0DH, 0EH, 10H (MW) |

Keep them at 0 unless you have a reason; then the addresses in Python are the
real MW/IW numbers.

### 3.4 Write range (PARAM12-13)

Only MW registers from LO to HI can be written over the network
(`0 <= LO <= HI <= highest MW`). It applies to 05H, 06H, 0BH, 0EH, 0FH and
10H. **Reads are not limited.** A write outside the range is refused with
error 02 **(tested)**. Example (tested 2026-10-27): PARAM12 = 5500,
PARAM13 = 10001 -> `read("MW00000")` works, `write("MW00000", ...)` is
refused even with the same value, writes to MW05500-MW10001 work. PARAM12 is
read by the running MSG-RCV, so changing it in the ladder takes effect
without a PLC restart:

```
IllegalAddress: controller rejected function 0x0B: error 0x02 (reference number
(address) error) - writing 1 words at 30000; the controller only accepts writes
inside its configured write range ...
```

There is one range per MSG-RCV (per connection). To allow two areas, widen
the range or use a second connection with its own MSG-RCV.

On the tested PLC, two drawings set this list differently (16300-16499 and
0-10001). Only 0-10001 was in effect (writes at MW16300 were refused,
MW00000-MW10001 accepted), so only one of them was really running for
connection 1.

### 3.5 Non-procedure receive (Pro-Typ = 2 or 3)

The list is shorter: PARAM02 connection, PARAM06 data size (OUT),
**PARAM12 register offset**, **PARAM13 write range HI**. Received data is
stored from MW(offset) upward (PARAM12 = 5500: first word goes to MW05500,
**tested**); anything past HI is discarded. PARAM06 tells the ladder how many
words arrived (510 for a full message, **tested**). There is no
reply to the sender. Python side:

```python
from yaskawa_coms import NonProcedureClient
with NonProcedureClient("192.168.1.1", 10003) as link:
    link.send_words([1, 2, 3])
```

---

## 4. MSG-SND reference (PLC sends)

Same inputs and outputs as MSG-RCV (Execute, Abort, Dev-Typ, Pro-Typ,
Cir-No, Ch-No, Param, Busy, Complete, Error). The difference is the
handshake:

* Turn **Execute ON and hold it** until Complete or Error turns ON.
* Then turn Execute **OFF for at least one scan** before the next send.
* Complete / Error stay ON for one scan only.

### 4.1 Parameter list (Pro-Typ = 1, PLC is the master)

| PARAM | Dir | Meaning |
|---|---|---|
| 00 | OUT | Processing result |
| 01 | OUT | Status |
| 02 | IN | Connection number 1-20 (which remote device) |
| 03 | IN | Option (not used for MEMOBUS) |
| 04 | IN | Function code to send (table below) |
| 05 | IN | Data address at the remote device (e.g. 1000 = MW01000) |
| 06 | IN | Data size (bits or words) |
| 07 | IN | Remote CPU number: 1 for an MP2000, else 0 |
| 08-11 | IN | Offsets on the **sending** PLC: where the data is taken from / stored |
| 12 | SYS | System. Set to 0 in the first scan |
| 13-16 | SYS | System |

Function codes and limits per message (manual p.5-60 and 5-62):

| Code | Function | 218IF-01 | 218IF-02 (BIN) |
|---|---|---|---|
| 01H / 02H | read coils / input relays | 1-2000 bits | 1-2000 bits |
| 03H / 04H | read holding / input registers | 1-125 words | 1-125 words |
| 05H / 06H | write one coil / register | 1 | 1 |
| 08H | loopback | - | - |
| 09H / 0AH | read holding / input (extended) | 1-508 | 1-2044 |
| 0BH | write holding (extended) | 1-507 | 1-2043 |
| 0DH / 0EH | read / write scattered (extended) | 508 / 254 | 2044 / 1022 |
| 0FH | write coils | 1-800 bits | 1-800 bits |
| 10H | write holding registers | 1-100 words | 1-100 words |

The tested PLC accepted 1020 words per read and 1019 per write from the PC
(the 218IF-02 ASCII figures), so check your own unit. `yaskawa-coms` lets
you set `max_read_words` / `max_write_words`.

### 4.2 Non-procedure send (Pro-Typ = 2 or 3)

PARAM02 connection, PARAM05 data address (first MW to send), PARAM06 size
(1-510 words on 218IF), PARAM11 register offset. The words go out low byte
first with no header and no reply. Python side:

```python
with NonProcedureClient("192.168.1.1", 10003) as link:
    data = link.receive_words(10)   # exactly the PARAM06 size
```

Over TCP, leave time between sends (the manual suggests 1 s or more) or
always receive a fixed size; otherwise two messages can arrive glued together.

---

## 5. Reading the result and status words

### 5.1 PARAM00 processing result (upper byte)

| Value | Meaning | Check |
|---|---|---|
| 00xxH | busy | |
| 10xxH | completed OK | |
| 81xxH | function code error | PARAM04 / the PC's request |
| 82xxH | address setting error | PARAM05, offsets PARAM08-11 |
| 83xxH | data size error | PARAM06 |
| 84xxH | circuit number error | Cir-No |
| 85xxH | channel number error | Ch-No |
| 86xxH | connection number error | PARAM02 (1-20) |
| 88xxH | transmission device error | cable, remote device |
| 89xxH | device selection error | Dev-Typ |

### 5.2 PARAM01 status

```
bit F      REQUEST   1 = request pending, 0 = accepted
bits E-C   RESULT    0 CONN_NG   connection failed / ended abnormally
                     1 SEND_OK   2 REC_OK   3 ABORT_OK
                     4 FMT_NG    parameter error (code in bits 7-0)
                     5 SEQ_NG    6 RESET_NG   7 REC_NG (receive error)
bits B-8   COMMAND   1 U_SEND  2 U_REC  3 ABORT
                     8 M_SEND  9 M_REC  C MR_SEND
bits 7-0   PARAMETER connection number, or when RESULT = 4 (FMT_NG):
                     01 connection no.  02 receive monitor time  03 retry count
                     04 cyclic area     05 CPU no.  06 data address
                     07 data size       08 function code
```

Decoding in Python, e.g. after reading DW00001 copied into an MW register by
the ladder:

```python
status = 0x2901
result  = (status >> 12) & 0x7    # 2 -> REC_OK
command = (status >> 8) & 0xF     # 9 -> M_REC (received a MEMOBUS command)
param   = status & 0xFF           # connection 1
```

---

## 6. Troubleshooting (seen from Python)

| Symptom in Python | Likely cause on the PLC |
|---|---|
| Protocol change has no effect | PLC not restarted after changing protocol type / Pro-Typ **(tested)** |
| `ResponseTimeout` on every request | MSG-RCV not running (drawing not called, Execute OFF), wrong connection number in PARAM02, or wrong port / TCP vs UDP |
| `CommunicationError: cannot connect` | No 218IF connection on that port, or node IP/port set to a different PC (use 0.0.0.0 / 0) |
| Connection drops when a second program connects | Unpassive open: a new connection replaces the old one. Use one client per connection |
| `IllegalAddress` on writes only | Address outside PARAM12-PARAM13 write range **(tested)** |
| `IllegalAddress` on reads | Address past the end of the MW area (MW32768 on the tested PLC) **(tested)** |
| `IllegalDataCount` | Request too big, or runs past the end of the area **(tested)** |
| A big write gets no reply, then the connection drops | Write larger than the controller accepts (over 1019 words on the tested PLC) **(tested)** |
| Write "doesn't stick" | The PLC program overwrites that register every scan **(tested on MW6551-6586)** |
| Data lands at the wrong address | Non-zero offsets PARAM08-11 |
| DW00025 (error counter) keeps rising | Look at PARAM00/PARAM01 for the reason |

The DW registers are local to their drawing and cannot be read over MEMOBUS.
To watch PARAM00/01 or the counters from Python, have the ladder copy them
into MW registers (e.g. `STORE DW00001 -> MW09000`) and read those.
