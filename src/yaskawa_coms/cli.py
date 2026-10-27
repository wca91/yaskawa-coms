"""Command-line tool.

    yaskawa-coms read  192.168.1.1:10001 MW00100 10
    yaskawa-coms write 192.168.1.1:10001 MF21000 8.68
    yaskawa-coms read  192.168.1.1:10002 MW00100 --protocol modbus
    yaskawa-coms simulate --port 10001
"""
from __future__ import annotations

import argparse
import sys
import time
from typing import List, Optional

from . import __version__
from .exceptions import YaskawaComError


def _host_port(text: str):
    host, sep, port = text.rpartition(":")
    if not sep or not port.isdigit():
        raise argparse.ArgumentTypeError("use HOST:PORT, e.g. 192.168.1.1:10001")
    return host, int(port)


def _value(text: str):
    low = text.lower()
    if low in ("true", "on"):
        return True
    if low in ("false", "off"):
        return False
    try:
        return int(text, 0)
    except ValueError:
        return float(text)


def _client(args):
    host, port = args.target
    common = dict(transport="udp" if args.udp else "tcp", timeout=args.timeout)
    if args.protocol == "modbus":
        from .modbus import ModbusTcpClient
        return ModbusTcpClient(host, port, unit_id=args.unit, **common)
    from .memobus import MemobusClient
    return MemobusClient(host, port, cpu=args.cpu, module=args.module, **common)


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="yaskawa-coms", description="Read/write Yaskawa controller registers.")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    def conn_args(sp):
        sp.add_argument("target", type=_host_port, help="HOST:PORT")
        sp.add_argument("--protocol", choices=["memobus", "modbus"], default="memobus")
        sp.add_argument("--udp", action="store_true", help="use UDP instead of TCP")
        sp.add_argument("--cpu", type=int, default=1, help="destination CPU (MEMOBUS, default 1)")
        sp.add_argument("--module", default="218IF-01", help="218IF-01 or 218IF-02 (MEMOBUS)")
        sp.add_argument("--unit", type=int, default=0, help="unit ID (Modbus, default 0)")
        sp.add_argument("--timeout", type=float, default=2.0)

    r = sub.add_parser("read", help="read registers")
    conn_args(r)
    r.add_argument("address", help="e.g. MW00100, MF21000, MB00100A, IW0010")
    r.add_argument("count", nargs="?", type=int, default=1)

    w = sub.add_parser("write", help="write registers")
    conn_args(w)
    w.add_argument("address")
    w.add_argument("values", nargs="+", type=_value)

    s = sub.add_parser("simulate", help="run a local controller simulator")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=10001)
    s.add_argument("--protocol", choices=["memobus", "modbus"], default="memobus")
    s.add_argument("--udp", action="store_true")

    args = p.parse_args(argv)
    try:
        if args.command == "simulate":
            from .simulator import MemobusSimulator, ModbusSimulator
            cls = ModbusSimulator if args.protocol == "modbus" else MemobusSimulator
            with cls(args.host, args.port, "udp" if args.udp else "tcp") as sim:
                print(f"{args.protocol} simulator on {args.host}:{sim.port} ({'udp' if args.udp else 'tcp'}). Ctrl+C to stop.")
                try:
                    while True:
                        time.sleep(1)
                except KeyboardInterrupt:
                    pass
            return 0
        with _client(args) as plc:
            if args.command == "read":
                values = plc.read(args.address, args.count)
                from .address import parse_address
                a = parse_address(args.address)
                step = a.width if a.kind != "B" else 1
                for i, v in enumerate(values):
                    if a.kind == "B":
                        label = f"bit {a.coil + i}"
                    else:
                        label = str(type(a)(a.area, a.kind, a.word + i * step))
                    print(f"{label}\t{v}")
            else:
                plc.write(args.address, args.values if len(args.values) > 1 else args.values[0])
                print("ok")
        return 0
    except YaskawaComError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
