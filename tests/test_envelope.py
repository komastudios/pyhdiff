import hashlib
import struct

import pytest

import pyhdiff
from pyhdiff import raw

BASE = b"".join(b"record %06d state %d\n" % (i, i % 7) for i in range(4000))
TARGET = BASE.replace(b"state 3", b"state 4")
PROFILES = [pyhdiff.HDiffProfile(), pyhdiff.ZstdProfile(), pyhdiff.ZstdProfile(level=19)]


@pytest.mark.parametrize("profile", PROFILES, ids=repr)
@pytest.mark.parametrize(("base", "target"), [(b"", b""), (b"", b"abc"), (b"abc", b""),
                                              (BASE, TARGET), (BASE, BASE)])
def test_roundtrip(profile, base, target):
    blob = pyhdiff.encode_delta(base, target, profile)
    header = pyhdiff.inspect(blob)
    assert header.format_version == 1
    assert header.codec == (pyhdiff.Codec.HDIFF if isinstance(profile, pyhdiff.HDiffProfile)
                            else pyhdiff.Codec.ZSTD)
    assert (header.base_length, header.target_length) == (len(base), len(target))
    assert header.payload_length == len(blob) - 96
    assert header.base_sha256 == hashlib.sha256(base).digest()
    assert header.target_sha256 == hashlib.sha256(target).digest()
    assert pyhdiff.apply(base, blob) == target
    assert pyhdiff.apply(bytearray(base), memoryview(blob)) == target


def test_layout():
    blob = pyhdiff.encode_delta(BASE, TARGET)
    magic, version, codec, reserved, bn, tn, pn = struct.unpack(">4sBBHQQQ", blob[:32])
    assert (magic, version, codec, reserved) == (b"PHDF", 1, 1, 0)
    assert (bn, tn, pn) == (len(BASE), len(TARGET), len(blob) - 96)
    assert blob[96:] == raw.hdiff_encode(BASE, TARGET)


def test_encode_base_is_standalone_zstd():
    blob = pyhdiff.encode_base(TARGET)
    header = pyhdiff.inspect(blob)
    assert header.codec == pyhdiff.Codec.ZSTD and header.base_length == 0
    assert blob[96:] == pyhdiff.compress(TARGET, 3)
    assert pyhdiff.decompress(blob[96:], len(TARGET)) == TARGET
    assert pyhdiff.apply(b"", blob) == TARGET
    with pytest.raises(TypeError):
        pyhdiff.encode_base(TARGET, pyhdiff.HDiffProfile())


def edit(blob, offset, value):
    return blob[:offset] + value + blob[offset + len(value):]


@pytest.mark.parametrize(
    ("offset", "value"),
    [(0, b"XXXX"), (4, b"\x02"), (5, b"\x03"), (5, b"\x00"), (6, b"\x00\x01"), (24, b"\xff")],
)
def test_malformed_header(offset, value):
    blob = edit(pyhdiff.encode_delta(BASE, TARGET), offset, value)
    with pytest.raises(pyhdiff.EnvelopeError):
        pyhdiff.inspect(blob)
    with pytest.raises(pyhdiff.EnvelopeError):
        pyhdiff.apply(BASE, blob)


@pytest.mark.parametrize("profile", PROFILES, ids=repr)
def test_every_truncation_and_extension_fails(profile):
    blob = pyhdiff.encode_delta(BASE, TARGET, profile)
    for n in range(len(blob)):
        with pytest.raises(pyhdiff.Error):
            pyhdiff.apply(BASE, blob[:n])
    with pytest.raises(pyhdiff.EnvelopeError):
        pyhdiff.apply(BASE, blob + b"\0")


@pytest.mark.parametrize("profile", PROFILES, ids=repr)
def test_payload_length_lies(profile):
    # A consistent header over a shortened or extended payload must fail decoding.
    blob = pyhdiff.encode_delta(BASE, TARGET, profile)
    for payload in (blob[96:-1], blob[96:] + b"\0", blob[96:] + blob[96:]):
        forged = blob[:24] + struct.pack(">Q", len(payload)) + blob[32:96] + payload
        with pytest.raises((pyhdiff.DecodeError, pyhdiff.IntegrityError)):
            pyhdiff.apply(BASE, forged)


def test_wrong_base():
    blob = pyhdiff.encode_delta(BASE, TARGET)
    with pytest.raises(pyhdiff.BaseMismatchError):
        pyhdiff.apply(BASE + b"x", blob)
    same_length = b"X" + BASE[1:]
    with pytest.raises(pyhdiff.BaseMismatchError):
        pyhdiff.apply(same_length, blob)


def test_target_hash_is_verified():
    blob = bytearray(pyhdiff.encode_delta(BASE, TARGET))
    blob[64] ^= 1
    with pytest.raises(pyhdiff.IntegrityError):
        pyhdiff.apply(BASE, bytes(blob))


def test_target_length_is_verified():
    blob = pyhdiff.encode_delta(BASE, TARGET, pyhdiff.ZstdProfile())
    forged = blob[:16] + struct.pack(">Q", len(TARGET) + 1) + blob[24:]
    with pytest.raises(pyhdiff.DecodeError):
        pyhdiff.apply(BASE, forged)


def test_output_limit():
    blob = pyhdiff.encode_delta(BASE, TARGET)
    with pytest.raises(pyhdiff.LimitError):
        pyhdiff.apply(BASE, blob, max_output=len(TARGET) - 1)
    assert pyhdiff.apply(BASE, blob, max_output=len(TARGET)) == TARGET


def test_mutations_never_succeed_wrongly():
    for profile in PROFILES:
        blob = pyhdiff.encode_delta(BASE, TARGET, profile)
        for offset in range(96, len(blob)):
            mutated = bytearray(blob)
            mutated[offset] ^= 0x55
            try:
                assert pyhdiff.apply(BASE, bytes(mutated)) == TARGET
            except pyhdiff.Error:
                pass


@pytest.mark.parametrize(
    "profile",
    [pyhdiff.HDiffProfile(match_score=65), pyhdiff.HDiffProfile(window_log=24),
     pyhdiff.HDiffProfile(level=0), pyhdiff.ZstdProfile(level=23),
     pyhdiff.ZstdProfile(window_log=31), pyhdiff.ZstdProfile(ldm="maybe"),
     pyhdiff.ZstdProfile(ldm_hash_log=10)],
    ids=repr,
)
def test_options_rejected(profile):
    with pytest.raises(pyhdiff.OptionError):
        pyhdiff.encode_delta(b"a", b"b", profile)


def test_types():
    with pytest.raises(TypeError):
        pyhdiff.encode_delta("text", b"b")
    with pytest.raises(TypeError):
        pyhdiff.apply(b"", 42)
    with pytest.raises(TypeError):
        pyhdiff.encode_delta(b"a", b"b", object())


def test_raw_payloads():
    payload = raw.zstd_encode(BASE, TARGET, pyhdiff.ZstdProfile(level=19))
    assert raw.zstd_apply(BASE, payload, len(TARGET)) == TARGET
    payload = raw.hdiff_encode(BASE, TARGET)
    assert raw.hdiff_apply(BASE, payload, len(TARGET)) == TARGET
    with pytest.raises(pyhdiff.DecodeError):
        raw.hdiff_apply(BASE, payload, len(TARGET) + 1)


def test_exception_hierarchy():
    # Documented in the README; callers catch pyhdiff.Error for every codec failure.
    refusals = [pyhdiff.EnvelopeError, pyhdiff.BaseMismatchError, pyhdiff.DecodeError,
                pyhdiff.IntegrityError, pyhdiff.LimitError, pyhdiff.OptionError]
    assert not issubclass(pyhdiff.Error, ValueError)
    for error in refusals:
        assert issubclass(error, pyhdiff.Error) and issubclass(error, ValueError)
    assert issubclass(pyhdiff.NativeError, pyhdiff.Error)
    assert issubclass(pyhdiff.NativeError, RuntimeError)
    assert not issubclass(pyhdiff.NativeError, ValueError)
    assert issubclass(pyhdiff.AllocationError, pyhdiff.Error)
    assert issubclass(pyhdiff.AllocationError, MemoryError)
    assert not issubclass(pyhdiff.AllocationError, ValueError)
    for status, error in [(pyhdiff._native.STATUS_ALLOC, pyhdiff.AllocationError),
                          (pyhdiff._native.STATUS_EXCEPTION, pyhdiff.NativeError),
                          (pyhdiff._native.STATUS_UNKNOWN, pyhdiff.NativeError),
                          (pyhdiff._native.STATUS_LIMIT, pyhdiff.LimitError),
                          (pyhdiff._native.STATUS_INVALID, pyhdiff.DecodeError),
                          (pyhdiff._native.STATUS_OPTION, pyhdiff.OptionError)]:
        with pytest.raises(error):
            pyhdiff._raise(status, decoding=True)
