"""Every codec call must release the GIL, and this must be asserted, not assumed.

The check is deterministic and involves no timing. The switch interval is set
so long that the interpreter never hands the GIL to another thread on its
own; a helper thread counts only while it holds the GIL and gives the GIL back
after every increment. The helper can therefore run only while the main thread
has explicitly released the GIL. A GIL-holding call leaves the counter exactly
unchanged, however long it runs. A releasing call lets the counter advance; the
call repeats until that is observed, bounded by a number of attempts.
"""

import random
import sys
import threading
import time

import pytest

import pyhdiff
from pyhdiff import _native

ATTEMPTS = 1000
HDIFF = (6, 19, 23, 1024, 262144, 1, 0)
ZSTD = (19, 0, 0, 1, 1, 0, 0, 0, 0)


class Counter:
    def __init__(self):
        self.count = 0
        self._stop = False
        self._started = threading.Event()
        self._thread = threading.Thread(target=self._run)

    def _run(self):
        self._started.set()
        while not self._stop:
            self.count += 1
            time.sleep(0)  # give the GIL back; it is retaken only when free

    def __enter__(self):
        self._interval = sys.getswitchinterval()
        sys.setswitchinterval(1000.0)
        self._thread.start()
        self._started.wait()  # blocking wait releases the GIL
        return self

    def __exit__(self, *exc):
        self._stop = True
        self._thread.join()
        sys.setswitchinterval(self._interval)


def releases_gil(call):
    """True if some call lets the counter advance; False if none of them did."""
    with Counter() as counter:
        for _ in range(ATTEMPTS):
            before = counter.count
            call()
            if counter.count != before:
                return True
    return False


def holds_gil(call, repeats=20):
    """True if the counter stays exactly unchanged across every call."""
    with Counter() as counter:
        for _ in range(repeats):
            before = counter.count
            call()
            if counter.count != before:
                return False
    return True


rng = random.Random(5)
WORDS = [bytes(rng.choices(b"abcdefghij", k=8)) for _ in range(512)]
BASE = b" ".join(rng.choices(WORDS, k=40_000))
TARGET = BASE.replace(WORDS[0], b"*" * 8)
HDIFF_PAYLOAD = _native.hdiff_encode(BASE, TARGET, *HDIFF)
ZSTD_PAYLOAD = _native.zstd_encode(BASE, TARGET, *ZSTD)
FRAME = _native.compress(TARGET, 3, 0, 1)

# The public functions that add hashing are not listed: hashlib releases the
# GIL itself, which would make them pass without proving anything about pyhdiff.
CALLS = {
    "hdiff_encode": lambda: _native.hdiff_encode(BASE, TARGET, *HDIFF),
    "hdiff_apply": lambda: _native.hdiff_apply(BASE, HDIFF_PAYLOAD, 0, len(TARGET)),
    "zstd_encode": lambda: _native.zstd_encode(BASE, TARGET, *ZSTD),
    "zstd_apply": lambda: _native.zstd_apply(BASE, ZSTD_PAYLOAD, 0, len(TARGET)),
    "compress": lambda: _native.compress(TARGET, 3, 0, 1),
    "decompress": lambda: _native.decompress(FRAME, len(TARGET)),
    "pyhdiff.compress": lambda: pyhdiff.compress(TARGET),
    "pyhdiff.decompress": lambda: pyhdiff.decompress(FRAME, len(TARGET)),
}


def test_every_native_entry_point_is_covered():
    functions = {name for name in dir(_native)
                 if callable(getattr(_native, name)) and not isinstance(getattr(_native, name), type)}
    assert functions == {name for name in CALLS if "." not in name}


@pytest.mark.parametrize("name", CALLS)
def test_gil_released(name):
    assert releases_gil(CALLS[name])


def test_detector_rejects_a_gil_holding_call():
    # list.sort runs in C without releasing the GIL: a regression in the
    # extension would look exactly like this.
    values = [rng.random() for _ in range(20_000)]
    assert holds_gil(lambda: sorted(values))
    assert not releases_gil(lambda: sorted(values))
