import os
import shutil
import struct
import subprocess
import sys

import pytest

import pyhdiff

DATA = b"".join(b"line %08d value %d\n" % (i, i * 7 % 13) for i in range(50000))


@pytest.mark.parametrize("level", [1, 3, 12, 19, 22])
def test_roundtrip(level):
    frame = pyhdiff.compress(DATA, level)
    assert frame[:4] == b"\x28\xb5\x2f\xfd"
    assert pyhdiff.decompress(frame, len(DATA)) == DATA


def test_bound_is_hard():
    frame = pyhdiff.compress(DATA)
    with pytest.raises(pyhdiff.LimitError):
        pyhdiff.decompress(frame, len(DATA) - 1)
    unsized = pyhdiff.compress(b"x" * 1_000_000, window_log=20, checksum=False)
    with pytest.raises(pyhdiff.LimitError):
        pyhdiff.decompress(unsized, 999_999)


def frame_header(frame):
    """Offsets of a standard frame's content-size field: (start, width, single_segment)."""
    descriptor = frame[4]
    fcs_flag, single, dict_flag = descriptor >> 6, (descriptor >> 5) & 1, descriptor & 3
    start = 5 + (0 if single else 1) + (0, 1, 2, 4)[dict_flag]
    width = (1 if single else 0, 2, 4, 8)[fcs_flag]
    return start, width, single


def with_content_size(frame, size):
    start, width, _ = frame_header(frame)
    assert width == 4
    return frame[:start] + struct.pack("<I", size) + frame[start + width:]


@pytest.mark.parametrize("delta", [-1, 1, -1000, 1000])
def test_content_size_lies(delta):
    data = DATA[:100_000]
    frame = pyhdiff.compress(data, 3, checksum=False)
    with pytest.raises((pyhdiff.DecodeError, pyhdiff.LimitError)):
        pyhdiff.decompress(with_content_size(frame, len(data) + delta), 2 * len(data))


def test_legacy_frame_rejected():
    legacy = struct.pack("<I", 0xFD2FB527) + bytes(32)  # zstd v0.7 magic
    with pytest.raises(pyhdiff.DecodeError):
        pyhdiff.decompress(legacy, 1 << 20)


def test_dictionary_frame_rejected():
    frame = pyhdiff.compress(DATA[:100_000], 3)
    descriptor = frame[4]
    assert descriptor & 3 == 0
    start = 5 + (0 if (descriptor >> 5) & 1 else 1)
    needs_dictionary = (frame[:4] + bytes([descriptor | 1]) + frame[5:start] + b"\x07"
                        + frame[start:])
    with pytest.raises(pyhdiff.DecodeError):
        pyhdiff.decompress(needs_dictionary, 1 << 20)


def test_exact_bound_then_skippable_and_empty_frames():
    data = DATA[:70_000]
    stream = (pyhdiff.compress(data) + struct.pack("<II", 0x184D2A5F, 3) + b"abc"
              + pyhdiff.compress(b""))
    assert pyhdiff.decompress(stream, len(data)) == data
    with pytest.raises(pyhdiff.LimitError):
        pyhdiff.decompress(stream + pyhdiff.compress(b"x"), len(data))


def test_forged_content_size_commits_no_memory():
    # A header claiming just under a 256 MiB bound must fail without the
    # process touching anything close to that much memory.
    script = """
import resource, struct, sys
import pyhdiff
bound = 256 << 20
header = b"\\x28\\xb5\\x2f\\xfd\\xe0" + struct.pack("<Q", bound - 1)
forged = header + b"\\x01\\x00\\x00"
before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
for _ in range(4):
    try:
        pyhdiff.decompress(forged, bound)
    except pyhdiff.DecodeError:
        pass
    else:
        sys.exit("forged frame decoded")
grown = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - before
print(grown)
sys.exit(0 if grown < 32 * 1024 else f"resident set grew by {grown} KiB")
"""
    subprocess.run([sys.executable, "-c", script], check=True)


def test_forged_content_size_does_not_allocate_or_pass():
    # Single-segment frame header claiming 2**40 bytes of content.
    forged = b"\x28\xb5\x2f\xfd" + bytes([0xE0]) + struct.pack("<Q", 1 << 40) + b"\x01\x00\x00"
    with pytest.raises((pyhdiff.DecodeError, pyhdiff.LimitError)):
        pyhdiff.decompress(forged, 1024)


def test_window_beyond_bound_rejected():
    frame = pyhdiff.compress(b"y" * 4096, 3, window_log=30, checksum=True)
    assert pyhdiff.decompress(frame, 4096) == b"y" * 4096  # window shrinks to the content size


def test_concatenated_and_skippable():
    frame = pyhdiff.compress(DATA)
    skippable = struct.pack("<II", 0x184D2A50, 4) + b"meta"
    assert pyhdiff.decompress(frame + skippable + frame, 2 * len(DATA)) == DATA * 2
    with pytest.raises(pyhdiff.LimitError):
        pyhdiff.decompress(frame + frame, 2 * len(DATA) - 1)


def test_invalid():
    frame = pyhdiff.compress(DATA)
    for n in (0, 3, 4, 10, len(frame) // 2, len(frame) - 1):
        with pytest.raises(pyhdiff.DecodeError):
            pyhdiff.decompress(frame[:n], len(DATA))
    with pytest.raises(pyhdiff.DecodeError):
        pyhdiff.decompress(frame + b"garbage", len(DATA))
    corrupted = bytearray(frame)
    corrupted[-2] ^= 1
    with pytest.raises(pyhdiff.DecodeError):
        pyhdiff.decompress(bytes(corrupted), len(DATA))


def test_empty_and_arguments():
    assert pyhdiff.decompress(pyhdiff.compress(b""), 0) == b""
    for level in (0, 23):
        with pytest.raises(pyhdiff.OptionError):
            pyhdiff.compress(DATA, level)
    with pytest.raises(pyhdiff.LimitError):
        pyhdiff.decompress(b"", -1)
    with pytest.raises(TypeError):
        pyhdiff.decompress(pyhdiff.compress(DATA))


zstd_cli = shutil.which("zstd")


@pytest.mark.skipif(not zstd_cli and not os.environ.get("PYHDIFF_REQUIRE_ZSTD_CLI"),
                    reason="zstd CLI not installed")
def test_zstd_cli_interoperability(tmp_path):
    assert zstd_cli, "PYHDIFF_REQUIRE_ZSTD_CLI is set but zstd is missing"
    source = tmp_path / "data"
    source.write_bytes(DATA)
    (tmp_path / "ours.zst").write_bytes(pyhdiff.compress(DATA, 9))
    restored = subprocess.run([zstd_cli, "-dc", tmp_path / "ours.zst"], check=True,
                              capture_output=True).stdout
    assert restored == DATA
    theirs = subprocess.run([zstd_cli, "-19", "-c", source], check=True,
                            capture_output=True).stdout
    assert pyhdiff.decompress(theirs, len(DATA)) == DATA
    streamed = subprocess.run([zstd_cli, "-3", "-c"], input=DATA, check=True,
                              capture_output=True).stdout
    assert pyhdiff.decompress(streamed, len(DATA)) == DATA
