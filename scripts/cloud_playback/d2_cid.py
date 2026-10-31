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
    return _encode_cid(codec, hash_type, digest)


def _encode_cid(codec, hash_type, digest):
    prefix = b"".join(_varint(value) for value in (CID_VERSION, codec, hash_type, len(digest)))
    return "b" + base64.b32encode(prefix + digest).decode().lower().rstrip("=")


def _cid_bytes(cid):
    text = cid[1:].upper()
    return base64.b32decode(text + "=" * (-len(text) % 8))


def _pb_field(number, wire_type, value):
    key = _varint((number << 3) | wire_type)
    if wire_type == PB_LENGTH_DELIMITED:
        return key + _varint(len(value)) + value
    return key + _varint(value)


DAG_PB_CODEC = 0x70
PB_VARINT = 0
PB_LENGTH_DELIMITED = 2
UNIXFS_FILE = 2
# D2 上传任务实测 block_size；content_identity 按这个大小切块
D2_FILE_BLOCK_SIZE = 4 * 1024 * 1024


def file_content_identity(source, *, block_size=D2_FILE_BLOCK_SIZE):
    """D2 ``content_identity``：单块为 raw CID，多块为一层 UnixFS dag-pb 根。

    2026-10-30 用 D2 上 1 块、2 块、13 块文件实测比对一致。``source`` 是二进制文件对象。
    """
    block_cids, block_sizes, total = [], [], 0
    while block := source.read(block_size):
        block_cids.append(block_cid(block))
        block_sizes.append(len(block))
        total += len(block)
    if not block_cids:
        raise D2Error("Cannot compute content identity of an empty file")
    if len(block_cids) == 1:
        return block_cids[0]
    unixfs = _pb_field(1, PB_VARINT, UNIXFS_FILE) + _pb_field(3, PB_VARINT, total)
    unixfs += b"".join(_pb_field(4, PB_VARINT, size) for size in block_sizes)
    links = b"".join(
        _pb_field(2, PB_LENGTH_DELIMITED,
                  _pb_field(1, PB_LENGTH_DELIMITED, _cid_bytes(cid))
                  + _pb_field(2, PB_LENGTH_DELIMITED, b"")
                  + _pb_field(3, PB_VARINT, size))
        for cid, size in zip(block_cids, block_sizes)
    )
    node = links + _pb_field(1, PB_LENGTH_DELIMITED, unixfs)
    return _encode_cid(DAG_PB_CODEC, DEFAULT_HASH_TYPE, hashlib.sha256(node).digest())
