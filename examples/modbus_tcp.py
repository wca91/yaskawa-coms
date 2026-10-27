"""Read and write an MP2000 controller over Modbus/TCP.

The 218IF connection must be set to protocol type MODBUS/TCP.

Usage:  python modbus_tcp.py 192.168.1.1 502
        python modbus_tcp.py --sim
"""
import sys

from yaskawa_coms import ModbusTcpClient, YaskawaComError


def demo(host: str, port: int) -> None:
    with ModbusTcpClient(host, port, unit_id=0) as plc:
        print("MW00000..003 =", plc.read("MW00000", 4))
        plc.write("MW00000", [10, 20, 30])
        print("coils MB00000 0..7 =", plc.read_coils(0, 8))
        print("after write  =", plc.read("MW00000", 4))


if __name__ == "__main__":
    try:
        if sys.argv[1:] == ["--sim"]:
            from yaskawa_coms.simulator import ModbusSimulator

            with ModbusSimulator() as sim:
                demo("127.0.0.1", sim.port)
        else:
            demo(sys.argv[1], int(sys.argv[2]))
    except YaskawaComError as exc:
        sys.exit(f"error: {exc}")
