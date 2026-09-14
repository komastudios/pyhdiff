#!/usr/bin/env python3
"""Cost of pyhdiff.compress across zstd levels on a large synthetic payload.

    python benchmarks/zstd_levels.py [--size-mib 198] [--levels 1,3,6,9,12,15,19]

The payload is JSON-like records with the redundancy of a typical structured
snapshot: sequential keys, a few enumerated fields, values drawn from small
pools. It compresses about 10x at level 3 and 17x at level 19, so the level curve has the shape
seen on real structured data of this size. Timings are single-threaded wall
clock on the machine running the script; measure on your own hardware before
choosing a level.
"""

import argparse
import json
import platform
import random
import time

import pyhdiff


def payload(size, seed=1):
    rng = random.Random(seed)
    states = ["alpha", "beta", "gamma", "delta", "epsilon"]
    groups = [f"g{n:03d}" for n in range(400)]
    values = [f"{rng.randrange(10**6):06d}" for _ in range(1500)]
    stamps = [f"2026-09-14T{rng.randrange(24):02d}:{rng.randrange(60):02d}:00Z" for _ in range(96)]
    # Records of one group share their attributes, as rows of one site or owner do.
    attrs = {g: (rng.choice(values), rng.choice(values)) for g in groups}
    parts, total, i = [], 2, 0
    while total < size:
        group = rng.choice(groups)
        record = ('{"key":"item-%07d","state":"%s","size":%d,"group":"%s","updated":"%s",'
                  '"attrs":{"a":"%s","b":"%s"}}' % (
                      i, rng.choice(states), rng.choice((11, 22, 50, 150, 350)), group,
                      rng.choice(stamps), *attrs[group]))
        parts.append(record)
        total += len(record) + 2
        i += 1
    return ("[" + ",\n".join(parts) + "]\n").encode()[:size]


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--size-mib", type=float, default=198)
    p.add_argument("--levels", default="1,3,6,9,12,15,19")
    p.add_argument("--repeat", type=int, default=1, help="best of N encodes per level")
    p.add_argument("--json", action="store_true", help="print one JSON document")
    a = p.parse_args()
    data = payload(int(a.size_mib * (1 << 20)))
    rows = []
    for level in (int(x) for x in a.levels.split(",")):
        encode = float("inf")
        for _ in range(a.repeat):
            start = time.perf_counter()
            frame = pyhdiff.compress(data, level)
            encode = min(encode, time.perf_counter() - start)
        start = time.perf_counter()
        assert pyhdiff.decompress(frame, len(data)) == data
        decode = time.perf_counter() - start
        rows.append({"level": level, "bytes": len(frame), "ratio": len(data) / len(frame),
                     "encode_s": encode, "decode_s": decode})
        if not a.json:
            print(f"level {level:2d}  {len(frame) / 2**20:8.2f} MiB  ratio {len(data) / len(frame):5.1f}"
                  f"  encode {encode:7.2f} s  decode {decode:5.2f} s", flush=True)
    if a.json:
        print(json.dumps({"input_bytes": len(data), "pyhdiff": pyhdiff.__version__,
                          "machine": platform.machine(), "processor": platform.processor(),
                          "results": rows}, indent=2))


if __name__ == "__main__":
    main()
