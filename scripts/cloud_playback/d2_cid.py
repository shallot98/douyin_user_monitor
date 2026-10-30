"""CID v1 raw blocks used by D2's documented upload implementation."""

import base64
import hashlib
import re

from .d2_client import D2Error

CID_VERSION = 1
DEFAULT_CODEC = 0x55
DEFAULT_HASH_TYPE = 0x12
HASH_NAMES = {0x11: "sha1", 0x12: "sha256", 0x13: "sha512"}
VARINT_DATA_BITS = 7
VARINT_DATA_MASK = 0x7F
VARINT_CONTINUATION = 0x80


def decimal_integer(value, name):
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
        return int(value)
    raise D2Error(f"D2 field {name} must be a nonnegative decimal integer")


def _varint(value):
    result = bytearray()
    while value > VARINT_DATA_MASK:
        result.append((value & VARINT_DATA_MASK) | VARINT_CONTINUATION)
        value >>= VARINT_DATA_BITS
    result.append(value)
    return bytes(result)


def block_cid(data, *, codec=DEFAULT_CODEC, hash_type=DEFAULT_HASH_TYPE):
    codec = decimal_integer(codec, "block_codec")
    hash_type = decimal_integer(hash_type, "block_hash_type")
    if hash_type not in HASH_NAMES:
        raise D2Error(f"D2 block hash type {hash_type} is unsupported")
    digest = hashlib.new(HASH_NAMES[hash_type], data).digest()
    prefix = b"".join(_varint(value) for value in (CID_VERSION, codec, hash_type, len(digest)))
    return "b" + base64.b32encode(prefix + digest).decode().lower().rstrip("=")
