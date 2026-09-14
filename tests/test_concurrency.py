from concurrent.futures import ThreadPoolExecutor

import pyhdiff

BASE = b"".join(b"%08d:%s\n" % (i, b"x" * (i % 17)) for i in range(40000))
TARGETS = [BASE.replace(b"x" * k, b"y" * k) for k in range(3, 11)]


def encode(target):
    delta = pyhdiff.encode_delta(BASE, target)
    frame = pyhdiff.encode_delta(BASE, target, pyhdiff.ZstdProfile(level=12))
    return delta, frame, pyhdiff.compress(target, 9)


def test_parallel_encodes_match_serial():
    serial = [encode(t) for t in TARGETS]
    with ThreadPoolExecutor(8) as pool:
        parallel = list(pool.map(encode, TARGETS * 2))
    assert parallel == serial * 2
    with ThreadPoolExecutor(8) as pool:
        restored = list(pool.map(lambda r: pyhdiff.apply(BASE, r[0]), parallel))
    assert restored == TARGETS * 2
