"""Every codec call must release the GIL, and this must be asserted, not assumed.

A spin thread counts loop iterations while the main thread is inside one long
native call. If the call held the GIL, the spin thread would be frozen for
the whole call; with the GIL released it keeps a large part of its idle rate.
Each call lasts far longer than the 5 ms interpreter switch interval, so a
GIL-holding call cannot pass by yielding between calls.
"""

import functools
import os
import random
import threading
import time

import pytest

import pyhdiff
from pyhdiff import _native

pytestmark = pytest.mark.skipif((os.cpu_count() or 1) < 2, reason="needs two CPUs")

MIN_CALL_SECONDS = 0.1
MIN_FRACTION = 0.3
HDIFF = (6, 19, 23, 1024, 262144, 1, 0)
HDIFF_FAST = (6, 1, 23, 0, 262144, 1, 0)
ZSTD19 = (19, 0, 0, 1, 1, 0, 0, 0, 0)


def spin_fraction(call):
    count = 0
    stop = threading.Event()

    def spin():
        nonlocal count
        while not stop.is_set():
            count += 1

    thread = threading.Thread(target=spin)
    thread.start()
    try:
        time.sleep(0.05)
        start_count, start = count, time.perf_counter()
        time.sleep(0.2)
        idle_rate = (count - start_count) / (time.perf_counter() - start)
        start_count, start = count, time.perf_counter()
        call()
        elapsed = time.perf_counter() - start
        busy_rate = (count - start_count) / elapsed
    finally:
        stop.set()
        thread.join()
    assert elapsed >= MIN_CALL_SECONDS, f"workload too short to judge: {elapsed:.3f} s"
    return busy_rate / idle_rate


@functools.cache
def text(size, seed):
    rng = random.Random(seed)
    words = [bytes(rng.choices(b"abcdefghij", k=8)) for _ in range(4096)]
    return b" ".join(rng.choices(words, k=size // 9))


@functools.cache
def edited(size):
    data = bytearray(text(size, 1))
    for i in range(0, len(data), 97):
        data[i] = 0x2A
    return bytes(data)


MIB = 1 << 20


def workloads():
    # Each call takes several tenths of a second on a current x86-64 core.
    small, other = text(MIB, 1), text(MIB, 2)
    big = text(128 * MIB, 3)
    medium = text(4 * MIB, 4)
    frame = _native.compress(big, 1, 0, 1)
    hdiff = _native.hdiff_encode(b"", big, *HDIFF_FAST)
    blob = pyhdiff.encode_delta(b"", big, pyhdiff.HDiffProfile(level=1, fast_block_bytes=0))
    return {
        "native hdiff_encode": lambda: _native.hdiff_encode(small, other, *HDIFF),
        "native hdiff_apply": lambda: _native.hdiff_apply(b"", hdiff, 0, len(big)),
        "native zstd_encode": lambda: _native.zstd_encode(small, edited(MIB), *ZSTD19),
        "native zstd_apply": lambda: _native.zstd_apply(b"", frame, 0, len(big)),
        "native compress": lambda: _native.compress(medium, 12, 0, 1),
        "native decompress": lambda: _native.decompress(frame, len(big)),
        "encode_delta": lambda: pyhdiff.encode_delta(small, other),
        "encode_base": lambda: pyhdiff.encode_base(medium, pyhdiff.ZstdProfile(level=12)),
        "apply": lambda: pyhdiff.apply(b"", blob),
        "compress": lambda: pyhdiff.compress(medium, 12),
        "decompress": lambda: pyhdiff.decompress(frame, len(big)),
    }


NAMES = ["native hdiff_encode", "native hdiff_apply", "native zstd_encode", "native zstd_apply",
         "native compress", "native decompress", "encode_delta", "encode_base", "apply",
         "compress", "decompress"]


@pytest.fixture(scope="module")
def calls():
    return workloads()


@pytest.mark.parametrize("name", NAMES)
def test_gil_released(name, calls):
    fraction = spin_fraction(calls[name])
    assert fraction > MIN_FRACTION, f"spin thread kept only {fraction:.0%} of its rate"


def test_detector_catches_a_gil_holding_call():
    # list.sort runs in C and keeps the GIL: the same measurement must fail it.
    rng = random.Random(9)
    values = [rng.random() for _ in range(3_000_000)]
    assert spin_fraction(lambda: sorted(values)) < 0.1
