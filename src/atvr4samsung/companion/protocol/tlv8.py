"""TLV8 codec for the HomeKit/HAP pairing process. Origin: pyatv v0.18.0 (MIT), adapted.

Note that this implementation only supports one level of value, i.e. no dicts
in dicts.
"""

# pylint: disable=invalid-name

from enum import IntEnum
from typing import List


class TlvValue(IntEnum):
    # Standardized HAP keys
    Method = 0x00
    Identifier = 0x01
    Salt = 0x02
    PublicKey = 0x03
    Proof = 0x04
    EncryptedData = 0x05
    SeqNo = 0x06
    Error = 0x07
    BackOff = 0x08
    Certificate = 0x09
    Signature = 0x0A
    Permissions = 0x0B
    FragmentData = 0x0C
    FragmentLast = 0x0D

    # Apple internal(?)
    Name = 0x11
    Flags = 0x13


class Flags(IntEnum):
    TransientPairing = 0x10


class ErrorCode(IntEnum):
    Unknown = 0x01
    Authentication = 0x02
    BackOff = 0x03
    MaxPeers = 0x04
    MaxTries = 0x05
    Unavailable = 0x06
    Busy = 0x07


class Method(IntEnum):
    PairSetup = 0x00
    PairSetupWithAuth = 0x01
    PairVerify = 0x02
    AddPairing = 0x03
    RemovePairing = 0x04
    ListPairing = 0x05


class State(IntEnum):
    M1 = 0x01
    M2 = 0x02
    M3 = 0x03
    M4 = 0x04
    M5 = 0x05
    M6 = 0x06


def read_tlv(data: bytes):
    """Parse TLV8 bytes, concatenating repeated tags used for values >255 bytes."""

    def _parse(data, pos, size, result=None):
        if result is None:
            result = {}
        if pos >= size:
            return result

        tag = int(data[pos])
        length = data[pos + 1]
        value = data[pos + 2 : pos + 2 + length]

        if tag in result:
            result[tag] += value
        else:
            result[tag] = value
        return _parse(data, pos + 2 + length, size, result)

    return _parse(data, 0, len(data))


def write_tlv(data: dict):
    """Write TLV8 bytes; this narrow codec expects byte values."""
    tlv = b""
    for key, value in data.items():
        tag = bytes([int(key)])
        length = len(value)
        pos = 0

        # A tag with length > 255 is added multiple times and concatenated into
        # one buffer when reading the TLV again.
        while pos < len(value):
            size = min(length, 255)
            tlv += tag
            tlv += bytes([size])
            tlv += value[pos : pos + size]
            pos += size
            length -= size
    return tlv


def stringify(data: dict) -> str:
    """Summarize selected TLV8 fields while reducing the rest to byte lengths."""

    def _enum_value_name(value: int, enum_type) -> str:
        try:
            return enum_type(value).name
        except ValueError:
            return hex(value)

    output: List[str] = []
    for key, value in data.items():
        key_type = TlvValue(key) if key in TlvValue.__members__.values() else None
        if key_type is None:
            output.append(f"{hex(key)}={len(value)}bytes")
        elif key_type == TlvValue.Method:
            method = int.from_bytes(value, byteorder="little")
            output.append(key_type.name + "=" + _enum_value_name(method, Method))
        elif key_type == TlvValue.SeqNo:
            seqno = int.from_bytes(value, byteorder="little")
            output.append(key_type.name + "=" + _enum_value_name(seqno, State))
        elif key_type == TlvValue.Error:
            code = int.from_bytes(value, byteorder="little")
            output.append(key_type.name + "=" + _enum_value_name(code, ErrorCode))
        elif key_type == TlvValue.BackOff:
            seconds = int.from_bytes(value, byteorder="little")
            output.append(f"{key_type.name}={seconds}s")
        else:
            output.append(f"{key_type.name}={len(value)}bytes")
    return ", ".join(output)
