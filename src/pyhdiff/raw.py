"""Codec payloads without the envelope, for callers that do their own framing.

These functions neither hash nor frame. apply-style functions need the exact
target length, which the caller must record; every other check that the
envelope provides (base identity, reconstruction integrity) is then the
caller's responsibility. The payload formats are the ones carried inside
envelopes, so the same compatibility guarantee covers them.
"""

from __future__ import annotations

from collections.abc import Buffer

from . import (
    RAW_MAX,
    HDiffProfile,
    LimitError,
    ZstdProfile,
    _bytes,
    _call,
    _hdiff_args,
    _native,
    _zstd_args,
)

__all__ = ["hdiff_apply", "hdiff_encode", "zstd_apply", "zstd_encode"]


def _inputs(base: Buffer, other: Buffer, name: str) -> tuple[bytes, bytes]:
    base, other = _bytes(base, "base"), _bytes(other, name)
    if len(base) > RAW_MAX:
        raise LimitError("base exceeds RAW_MAX")
    return base, other


def hdiff_encode(base: Buffer, target: Buffer, profile: HDiffProfile = HDiffProfile()) -> bytes:
    """An HDiffPatch single compressed diff of target against base."""
    base, target = _inputs(base, target, "target")
    return _call(False, _native.hdiff_encode, base, target, *_hdiff_args(profile))


def hdiff_apply(base: Buffer, payload: Buffer, target_length: int) -> bytes:
    """Reconstruct exactly target_length bytes from an HDiffPatch payload."""
    base, payload = _inputs(base, payload, "payload")
    if not 0 <= target_length <= RAW_MAX:
        raise LimitError("target_length must be between 0 and RAW_MAX")
    return _call(True, _native.hdiff_apply, base, payload, 0, target_length)


def zstd_encode(base: Buffer, target: Buffer, profile: ZstdProfile = ZstdProfile()) -> bytes:
    """One zstd frame of target, with a nonempty base referenced as a prefix."""
    base, target = _inputs(base, target, "target")
    return _call(False, _native.zstd_encode, base, target, *_zstd_args(profile))


def zstd_apply(base: Buffer, payload: Buffer, target_length: int) -> bytes:
    """Reconstruct exactly target_length bytes from one zstd frame and its prefix."""
    base, payload = _inputs(base, payload, "payload")
    if not 0 <= target_length <= RAW_MAX:
        raise LimitError("target_length must be between 0 and RAW_MAX")
    return _call(True, _native.zstd_apply, base, payload, 0, target_length)

