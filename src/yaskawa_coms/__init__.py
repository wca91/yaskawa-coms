"""yaskawa-coms: talk to Yaskawa MP-series controllers over Ethernet.

Three protocols, one style of API:

* :class:`MemobusClient` - Extended MEMOBUS (TCP or UDP)
* :class:`ModbusTcpClient` - Modbus/TCP
* :class:`NonProcedureClient` - raw MW register data, no protocol

Quick start::

    from yaskawa_coms import MemobusClient

    with MemobusClient("192.168.1.1", 10001) as plc:
        plc.read("MW00100")            # one signed 16-bit value
        plc.read("MW00100", 10)        # ten values
        plc.read("MF21000")            # float in MW21000/MW21001
        plc.write("MW00100", 1234)
        plc.write("MB00100A", True)    # bit A of MW00100

Not affiliated with or endorsed by Yaskawa Electric Corporation.
"""
from .address import Address, parse_address
from .datatypes import float_to_words, int32_to_words, words_to_float, words_to_int32
from .exceptions import (
    AddressError,
    CommunicationError,
    IllegalAddress,
    IllegalDataCount,
    IllegalFunction,
    ProtocolError,
    RemoteError,
    ResponseTimeout,
    YaskawaComError,
)
from .memobus import MemobusClient
from .modbus import ModbusTcpClient
from .nonprocedure import NonProcedureClient

__version__ = "0.1.0"

__all__ = [
    "Address",
    "AddressError",
    "CommunicationError",
    "IllegalAddress",
    "IllegalDataCount",
    "IllegalFunction",
    "MemobusClient",
    "ModbusTcpClient",
    "NonProcedureClient",
    "ProtocolError",
    "RemoteError",
    "ResponseTimeout",
    "YaskawaComError",
    "float_to_words",
    "int32_to_words",
    "parse_address",
    "words_to_float",
    "words_to_int32",
    "__version__",
]
