"""HDiffPatch deltas and zstd frames in a verified envelope.

The envelope is 96 bytes, all integers unsigned big-endian:

    offset 0    4 bytes   magic, ASCII "PHDF"
    offset 4    1 byte    format version, 1
    offset 5    1 byte    codec id, 1 = HDiffPatch, 2 = zstd
    offset 6    2 bytes   reserved, zero
    offset 8    8 bytes   base length
    offset 16   8 bytes   target length
    offset 24   8 bytes   payload length
    offset 32   32 bytes  SHA-256 of the base bytes
    offset 64   32 bytes  SHA-256 of the target bytes
    offset 96   variable  payload
"""

from __future__ import annotations

import enum
import hashlib
import struct
from collections.abc import Buffer
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from typing import Literal

from . import _native

__all__ = [
    "BaseMismatchError",
    "Codec",
    "DecodeError",
    "ENVELOPE_SIZE",
    "EnvelopeError",
    "Error",
    "FORMAT_VERSION",
    "FRAME_MAX",
    "HDiffProfile",
    "Header",
    "IntegrityError",
    "LimitError",
    "MAGIC",
    "NativeError",
    "OptionError",
    "PAYLOAD_MAX",
    "RAW_MAX",
    "ZstdProfile",
    "apply",
    "compress",
    "decompress",
    "encode_base",
    "encode_delta",
    "inspect",
]

try:
    __version__ = version("pyhdiff")
except PackageNotFoundError:  # pragma: no cover - source tree without metadata
    __version__ = "0+unknown"

MAGIC = b"PHDF"
FORMAT_VERSION = 1
ENVELOPE_SIZE = 96
RAW_MAX: int = _native.RAW_MAX
"""Largest base, target or reconstruction in bytes (256 MiB)."""
PAYLOAD_MAX: int = _native.PAYLOAD_MAX
"""Largest encoded payload in bytes; a complete envelope fits 512 MiB."""
FRAME_MAX: int = _native.FRAME_MAX
"""Largest input of compress and output of decompress in bytes (1 GiB)."""

_HEADER = struct.Struct(">4sBBHQQQ32s32s")
assert _HEADER.size == ENVELOPE_SIZE


class Error(Exception):
    """Base class of every pyhdiff failure."""


class EnvelopeError(Error, ValueError):
    """The bytes are not a well-formed envelope of a supported version."""


class BaseMismatchError(Error, ValueError):
    """The base differs in length or SHA-256 from the one the delta was made against."""


class DecodeError(Error, ValueError):
    """The payload or frame is malformed and cannot be decoded."""


class IntegrityError(Error, ValueError):
    """The decoded bytes differ in SHA-256 from the recorded target."""


class LimitError(Error, ValueError):
    """An input, output or window exceeds a size limit."""


class OptionError(Error, ValueError):
    """A profile parameter is outside its accepted range."""


class NativeError(Error, RuntimeError):
    """The native codec raised an internal exception; it was contained."""


class Codec(enum.IntEnum):
    HDIFF = 1
    ZSTD = 2


@dataclass(frozen=True, slots=True)
class HDiffProfile:
    """An HDiffPatch single compressed diff whose streams are zstd compressed.

    Accepted ranges: match_score 0-64, level 1-22, window_log 10-23,
    fast_block_bytes 0 (disabled) or 4-1048576, step_bytes 4096-262144.
    """

    match_score: int = 6
    level: int = 19
    window_log: int = 23
    fast_block_bytes: int = 1024
    step_bytes: int = 262144
    content_size: bool = True
    checksum: bool = False


@dataclass(frozen=True, slots=True)
class ZstdProfile:
    """A single zstd frame; a nonempty base is referenced as a prefix.

    Accepted ranges: level 1-22; window_log 0 (automatic) or 10-30; the LDM
    tuning values 0 (automatic) or ldm_hash_log 6-23, ldm_min_match 4-4096,
    ldm_bucket_log 1-8, ldm_rate_log 1-24. LDM tuning requires ldm="on".
    """

    level: int = 3
    window_log: int = 0
    ldm: Literal["auto", "on", "off"] = "auto"
    content_size: bool = True
    checksum: bool = True
    ldm_hash_log: int = 0
    ldm_min_match: int = 0
    ldm_bucket_log: int = 0
    ldm_rate_log: int = 0


@dataclass(frozen=True, slots=True)
class Header:
    format_version: int
    codec: Codec
    base_length: int
    target_length: int
    payload_length: int
    base_sha256: bytes
    target_sha256: bytes


_LDM = {"auto": 0, "on": 1, "off": 2}


def _bytes(value: Buffer, name: str) -> bytes:
    # Codecs run without the GIL, so they only ever see immutable bytes.
    if type(value) is bytes:
        return value
    if isinstance(value, str) or not isinstance(value, Buffer):
        raise TypeError(f"{name} must be a bytes-like object")
    return bytes(value)


def _raise(code: int, decoding: bool) -> None:
    if code == _native.STATUS_LIMIT:
        raise LimitError("size limit exceeded")
    if code == _native.STATUS_OPTION:
        raise OptionError("profile parameter out of range")
    if code == _native.STATUS_ALLOC:
        raise MemoryError("native allocation failed")
    if code == _native.STATUS_INVALID:
        raise DecodeError("malformed payload")
    if code >= _native.STATUS_ZSTD_ERROR_BASE:
        zstd = code - _native.STATUS_ZSTD_ERROR_BASE
        if zstd == 16:  # ZSTD_error_frameParameter_windowTooLarge
            raise LimitError("zstd window exceeds the decoder limit")
        if decoding:
            raise DecodeError(f"malformed zstd data (zstd error {zstd})")
        raise NativeError(f"zstd error {zstd}")
    raise NativeError(f"native codec failure (status {code})")


def _call(decoding: bool, function, *args) -> bytes:
    try:
        return function(*args)
    except _native.Error as error:
        _raise(error.args[0], decoding)
        raise  # unreachable


def _hdiff_args(profile: HDiffProfile) -> tuple[int, ...]:
    return (
        profile.match_score,
        profile.level,
        profile.window_log,
        profile.fast_block_bytes,
        profile.step_bytes,
        int(profile.content_size),
        int(profile.checksum),
    )


def _zstd_args(profile: ZstdProfile) -> tuple[int, ...]:
    if profile.ldm not in _LDM:
        raise OptionError("ldm must be 'auto', 'on' or 'off'")
    return (
        profile.level,
        profile.window_log,
        _LDM[profile.ldm],
        int(profile.content_size),
        int(profile.checksum),
        profile.ldm_hash_log,
        profile.ldm_min_match,
        profile.ldm_bucket_log,
        profile.ldm_rate_log,
    )


def _envelope(codec: Codec, base: bytes, target: bytes, payload: bytes) -> bytes:
    header = _HEADER.pack(
        MAGIC,
        FORMAT_VERSION,
        codec,
        0,
        len(base),
        len(target),
        len(payload),
        hashlib.sha256(base).digest(),
        hashlib.sha256(target).digest(),
    )
    return header + payload


def encode_delta(
    base: Buffer, payload: Buffer, profile: HDiffProfile | ZstdProfile = HDiffProfile()
) -> bytes:
    """Encode payload as a delta against base, in an envelope.

    An HDiffProfile produces codec 1; a ZstdProfile produces codec 2 with the
    base as a zstd prefix. Encoding is CPU-bound and cannot be interrupted;
    bound its cost by refusing oversized inputs before calling.
    """
    base = _bytes(base, "base")
    target = _bytes(payload, "payload")
    if len(base) > RAW_MAX or len(target) > RAW_MAX:
        raise LimitError("input exceeds RAW_MAX")
    if isinstance(profile, HDiffProfile):
        encoded = _call(False, _native.hdiff_encode, base, target, *_hdiff_args(profile))
        return _envelope(Codec.HDIFF, base, target, encoded)
    if isinstance(profile, ZstdProfile):
        encoded = _call(False, _native.zstd_encode, base, target, *_zstd_args(profile))
        return _envelope(Codec.ZSTD, base, target, encoded)
    raise TypeError("profile must be an HDiffProfile or a ZstdProfile")


def encode_base(payload: Buffer, profile: ZstdProfile = ZstdProfile()) -> bytes:
    """Encode payload as a standalone zstd frame in an envelope with an empty base."""
    if not isinstance(profile, ZstdProfile):
        raise TypeError("profile must be a ZstdProfile")
    return encode_delta(b"", payload, profile)


def inspect(blob: Buffer) -> Header:
    """Parse and validate a complete envelope without decoding its payload."""
    if isinstance(blob, str) or not isinstance(blob, Buffer):
        raise TypeError("blob must be a bytes-like object")
    with memoryview(blob) as view:
        size = view.nbytes
        if size < ENVELOPE_SIZE:
            raise EnvelopeError("shorter than the envelope header")
        with view.cast("B") as octets:
            fields = _HEADER.unpack_from(octets)
    magic, fmt, codec, reserved, base_len, target_len, payload_len, base_hash, target_hash = fields
    if magic != MAGIC:
        raise EnvelopeError("bad magic")
    if fmt != FORMAT_VERSION:
        raise EnvelopeError(f"unsupported format version {fmt}")
    if codec not in (Codec.HDIFF, Codec.ZSTD):
        raise EnvelopeError(f"unsupported codec {codec}")
    if reserved:
        raise EnvelopeError("reserved bytes are not zero")
    if payload_len != size - ENVELOPE_SIZE:
        raise EnvelopeError("payload length does not match the envelope size")
    return Header(fmt, Codec(codec), base_len, target_len, payload_len, base_hash, target_hash)


def apply(base: Buffer, blob: Buffer, *, max_output: int = RAW_MAX) -> bytes:
    """Reconstruct the target of an envelope and verify both SHA-256 hashes.

    Raises BaseMismatchError before decoding if base is not the recorded base,
    and IntegrityError if the reconstruction differs from the recorded target.
    """
    base = _bytes(base, "base")
    blob = _bytes(blob, "blob")
    header = inspect(blob)
    if header.base_length != len(base):
        raise BaseMismatchError("base length differs from the envelope")
    if header.target_length > min(max_output, RAW_MAX):
        raise LimitError("target length exceeds the output limit")
    if hashlib.sha256(base).digest() != header.base_sha256:
        raise BaseMismatchError("base SHA-256 differs from the envelope")
    decode = _native.hdiff_apply if header.codec == Codec.HDIFF else _native.zstd_apply
    target = _call(True, decode, base, blob, ENVELOPE_SIZE, header.target_length)
    if hashlib.sha256(target).digest() != header.target_sha256:
        raise IntegrityError("reconstruction SHA-256 differs from the envelope")
    return target


def compress(data: Buffer, level: int = 3, window_log: int = 0, *, checksum: bool = True) -> bytes:
    """Compress into one standard zstd frame, without an envelope.

    level 1-22; window_log 0 selects the level default, else 10-30. The frame
    records its content size, and a checksum unless checksum is False.
    """
    data = _bytes(data, "data")
    if len(data) > FRAME_MAX:
        raise LimitError("input exceeds FRAME_MAX")
    return _call(False, _native.compress, data, level, window_log, int(checksum))


def decompress(data: Buffer, max_output_bytes: int) -> bytes:
    """Decompress standard zstd frames with a hard bound on the output size.

    Accepts concatenated and skippable frames. Raises LimitError as soon as
    the output would exceed max_output_bytes, whatever the frame header claims.
    """
    data = _bytes(data, "data")
    if not 0 <= max_output_bytes <= FRAME_MAX:
        raise LimitError("max_output_bytes must be between 0 and FRAME_MAX")
    return _call(True, _native.decompress, data, max_output_bytes)
