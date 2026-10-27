"""Read and write registers over Extended MEMOBUS.

Usage:  python memobus_basic.py 192.168.1.1 10001
        python memobus_basic.py --sim          (no controller needed)
"""
import sys

from yaskawa_coms import MemobusClient, YaskawaComError


def demo(host: str, port: int) -> None:
    with MemobusClient(host, port, timeout=2.0) as plc:
        print("MW00100..104 =", plc.read("MW00100", 5))
        print("MF21000      =", plc.read("MF21000"))

        plc.write("MW00100", 1234)
        plc.write("MB00101A", True)          # bit A of MW00101
        print("after write  =", plc.read("MW00100", 2))


if __name__ == "__main__":
    try:
        if sys.argv[1:] == ["--sim"]:
            from yaskawa_coms.simulator import MemobusSimulator

            with MemobusSimulator() as sim:
                sim.memory.set("MF21000", 8.6809)
                demo("127.0.0.1", sim.port)
        else:
            demo(sys.argv[1], int(sys.argv[2]))
    except YaskawaComError as exc:
        sys.exit(f"error: {exc}")
