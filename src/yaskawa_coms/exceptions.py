"""Exceptions raised by yaskawa-coms.

Every error the library raises is a subclass of :class:`YaskawaComError`, so
callers can catch one type when they don't care about the detail.
"""
from __future__ import annotations


class YaskawaComError(Exception):
    """Base class for all yaskawa-coms errors."""


class AddressError(YaskawaComError, ValueError):
    """A register address string or number is invalid (e.g. ``"MX100"``)."""


class CommunicationError(YaskawaComError):
    """The connection failed, was closed, or no response arrived."""


class ResponseTimeout(CommunicationError):
    """The controller did not answer within the timeout."""


class ProtocolError(YaskawaComError):
    """A response arrived but does not match the request (bad length, wrong
    serial number, wrong function code, ...)."""


class RemoteError(YaskawaComError):
    """The controller answered with an error response.

    Attributes:
        function: the function code that was rejected.
        code: the error code returned by the controller.
    """

    #: Human-readable meaning of each error code (Extended MEMOBUS, p.A-23,
    #: and the standard Modbus exception codes).
    MEANINGS = {
        0x01: "function code error (not supported)",
        0x02: "reference number (address) error",
        0x03: "number of data items error",
        0x04: "slave device failure",
        0x05: "acknowledge (processing)",
        0x06: "slave device busy",
        0x0A: "gateway path unavailable",
        0x0B: "gateway target failed to respond",
    }

    def __init__(self, function: int, code: int):
        self.function = function
        self.code = code
        meaning = self.MEANINGS.get(code, "unknown error")
        super().__init__(
            f"controller rejected function 0x{function:02X}: error 0x{code:02X} ({meaning})"
        )


class IllegalFunction(RemoteError):
    """Error code 01: the controller does not support this function code."""


class IllegalAddress(RemoteError):
    """Error code 02: the register address is out of range."""


class IllegalDataCount(RemoteError):
    """Error code 03: the number of registers / data is out of range."""


_REMOTE_ERRORS = {0x01: IllegalFunction, 0x02: IllegalAddress, 0x03: IllegalDataCount}


def remote_error(function: int, code: int) -> RemoteError:
    """Build the most specific :class:`RemoteError` subclass for ``code``."""
    return _REMOTE_ERRORS.get(code, RemoteError)(function, code)
