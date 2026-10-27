"""Exchange raw MW data with a PLC program that uses MSG-SND / MSG-RCV in
non-procedure mode (protocol type "None", PRO-TYP = 2).

Usage:  python nonprocedure.py 192.168.1.1 10003
"""
import sys

from yaskawa_coms import NonProcedureClient, YaskawaComError

if __name__ == "__main__":
    host, port = sys.argv[1], int(sys.argv[2])
    try:
        with NonProcedureClient(host, port, transport="tcp", code="binary", timeout=5) as link:
            link.send_words([100, 200, 300])   # PLC's MSG-RCV stores these in MW
            print("received:", link.receive_words(3))  # 3 words from the PLC's MSG-SND
    except YaskawaComError as exc:
        sys.exit(f"error: {exc}")
