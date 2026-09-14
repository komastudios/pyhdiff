"""pyhdiff's private libc++ must coexist with a libstdc++ module in one process.

Python processes often load C++ extensions built against libstdc++. Each
ordering loads such a probe globally (RTLD_GLOBAL, the worst case for symbol
interposition) before or after pyhdiff, then drives C++ exceptions through
both runtimes: the probe's own, and the bridge's internal failures on every
truncation of a delta.
"""

import os
import subprocess
import sys

import pytest

PROBE = os.environ.get("PYHDIFF_STDCXX_PROBE")
pytestmark = pytest.mark.skipif(not PROBE, reason="set PYHDIFF_STDCXX_PROBE to a built std_probe.so")

SCRIPT = """
import ctypes, os, sys
sys.setdlopenflags(os.RTLD_NOW | os.RTLD_GLOBAL)
probe_first = sys.argv[2] == "probe-first"
if probe_first:
    probe = ctypes.CDLL(sys.argv[1], mode=os.RTLD_NOW | os.RTLD_GLOBAL)
import pyhdiff
from pyhdiff import raw
if not probe_first:
    probe = ctypes.CDLL(sys.argv[1], mode=os.RTLD_NOW | os.RTLD_GLOBAL)
assert probe.std_probe() == 1
base = bytes(range(256)) * 400
target = base.replace(bytes([7, 8, 9]), b"xyz")
for profile in (pyhdiff.HDiffProfile(), pyhdiff.ZstdProfile(level=19)):
    blob = pyhdiff.encode_delta(base, target, profile)
    assert pyhdiff.apply(base, blob) == target
payload = raw.hdiff_encode(base, target)
for n in range(len(payload)):
    try:
        raw.hdiff_apply(base, payload[:n], len(target))
    except pyhdiff.DecodeError:
        pass
    else:
        raise SystemExit(f"truncation {n} decoded")
assert probe.std_probe() == 1
print("coexist", sys.argv[2])
"""


@pytest.mark.parametrize("order", ["probe-first", "pyhdiff-first"])
def test_runtimes_coexist(order):
    result = subprocess.run([sys.executable, "-c", SCRIPT, PROBE, order],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert f"coexist {order}" in result.stdout
