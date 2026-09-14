# pyhdiff

Binary deltas and zstd compression for Python, with a verified envelope.

pyhdiff wraps [HDiffPatch](https://github.com/sisong/HDiffPatch) and
[zstd](https://github.com/facebook/zstd) behind a small C ABI. It encodes a
target as a delta against a base, or as a standalone zstd frame. It stores the
result in a 96-byte envelope that records both inputs' lengths and SHA-256
hashes, and reconstructs the target only after checking both. It also
exposes plain zstd `compress` and `decompress` for standard frames.

Releases are self-contained wheels for x86-64 Linux, fetched by URL and
verified by digest. The file format carries a compatibility guarantee: see
[Compatibility](#compatibility).

## Install

Each [release](https://github.com/komastudios/pyhdiff/releases) publishes one
wheel, `pyhdiff-X.Y.Z-cp312-abi3-manylinux_2_34_x86_64.whl`. It installs on
CPython 3.12 or newer, on x86-64 Linux with glibc 2.34 or newer. Its digest
appears in the release notes, in `SHA256SUMS` and in
`pyhdiff-X.Y.Z-digests.json`. Pin the exact URL and digest:

```toml
# pyproject.toml, uv: uv.lock records and checks the sha256
[tool.uv.sources]
pyhdiff = { url = "https://github.com/komastudios/pyhdiff/releases/download/v0.1.0/pyhdiff-0.1.0-cp312-abi3-manylinux_2_34_x86_64.whl" }
```

```text
# requirements.txt, pip --require-hashes
pyhdiff @ https://github.com/komastudios/pyhdiff/releases/download/v0.1.0/pyhdiff-0.1.0-cp312-abi3-manylinux_2_34_x86_64.whl --hash=sha256:<digest from the release>
```

The wheel links HDiffPatch, zstd and a hardened libc++ statically, and it
depends on nothing at runtime beyond glibc. It exports a single symbol,
`PyInit__native`.

## Usage

```python
import pyhdiff

base = open("snapshot-1.json", "rb").read()
target = open("snapshot-2.json", "rb").read()

checkpoint = pyhdiff.encode_base(base)            # zstd frame, empty base
delta = pyhdiff.encode_delta(base, target)        # HDiffPatch delta against base

assert pyhdiff.apply(b"", checkpoint) == base
assert pyhdiff.apply(base, delta) == target       # checks both hashes

header = pyhdiff.inspect(delta)                   # no decoding
header.codec, header.base_length, header.target_sha256.hex()

frame = pyhdiff.compress(target, level=12)        # a standard zstd frame
assert pyhdiff.decompress(frame, max_output_bytes=len(target)) == target
```

### Envelope functions

| Function | Result |
| --- | --- |
| `encode_base(payload, profile=ZstdProfile())` | Envelope, codec 2, empty base |
| `encode_delta(base, payload, profile=HDiffProfile())` | Envelope, codec 1 for an `HDiffProfile`, codec 2 with `base` as a zstd prefix for a `ZstdProfile` |
| `apply(base, blob, *, max_output=RAW_MAX)` | The target, after checking the base and the reconstruction |
| `inspect(blob)` | `Header(format_version, codec, base_length, target_length, payload_length, base_sha256, target_sha256)` |

`apply` raises `BaseMismatchError` before it decodes anything if `base`
differs from the recorded base in length or SHA-256. Applying a delta to the
wrong base is otherwise a silent corruption. It raises `IntegrityError` if
the reconstruction differs from the recorded target. `EnvelopeError`,
`DecodeError`, `LimitError` and `OptionError` cover malformed envelopes,
malformed payloads, size limits and profile values out of range. All of
these exceptions derive from `pyhdiff.Error` and `ValueError`.

Inputs can be any bytes-like object. Objects other than `bytes` are copied
once, because the codecs run without the GIL and must not see memory that
another thread could change.

### Profiles

Defaults reproduce the measured profiles byte for byte.

| `HDiffProfile` field | Default | Range |
| --- | ---: | --- |
| `match_score` | 6 | 0 – 64 |
| `level` (zstd, for the diff streams) | 19 | 1 – 22 |
| `window_log` | 23 | 10 – 23 |
| `fast_block_bytes` | 1024 | 0 (off), 4 – 1048576 |
| `step_bytes` | 262144 | 4096 – 262144 |
| `content_size`, `checksum` | `True`, `False` | |

| `ZstdProfile` field | Default | Range |
| --- | ---: | --- |
| `level` | 3 | 1 – 22 |
| `window_log` | 0 (automatic) | 10 – 30 |
| `ldm` | `"auto"` | `"auto"`, `"on"`, `"off"` |
| `content_size`, `checksum` | `True`, `True` | |
| `ldm_hash_log`, `ldm_min_match`, `ldm_bucket_log`, `ldm_rate_log` | 0 (automatic) | 6–23, 4–4096, 1–8, 1–24; need `ldm="on"` |

With a nonempty base and an automatic window, the zstd window follows the
target size, and long-distance matching turns on when the target outgrows
the level's chain log.

### zstd frames

`compress(data, level=3, window_log=0, *, checksum=True)` produces one
standard zstd frame. The frame records its content size and, by default, a
checksum. `decompress(data, max_output_bytes)` accepts concatenated and
skippable frames. The output bound is required and hard: `LimitError` is
raised as soon as the output would exceed it, whatever size the frame header
claims. A frame header's content size only reserves address space; memory
is committed as data actually decodes, so a forged header costs nothing. The
decoder refuses windows larger than the bound rounded up to a power of two,
except that 8 MiB windows, which streaming compressors use by default, are
always accepted. Decoding a frame therefore needs at most about the bound plus
its window in memory. The frames interoperate with
the `zstd` command-line tool and other zstd bindings. Legacy pre-1.0 formats
are not decoded.

### Choosing a zstd level

The level dominates encode time on large payloads. Past level 15, each step
buys a few percent of size for several times the time. Decode time barely
depends on the level. `benchmarks/zstd_levels.py` measures the curve on a
198 MiB synthetic structured payload. One run on an AMD Ryzen 9 5950X, shared with
other work, so treat the timings as a shape rather than a specification:

| Level | Output | Ratio | Encode | Decode |
| ---: | ---: | ---: | ---: | ---: |
| 1 | 19.4 MiB | 10.2 | 0.3 s | 0.89 s |
| 3 | 19.2 MiB | 10.3 | 0.3 s | 0.88 s |
| 6 | 18.1 MiB | 10.9 | 1.6 s | 0.87 s |
| 9 | 16.7 MiB | 11.8 | 2.8 s | 0.89 s |
| 12 | 15.0 MiB | 13.2 | 6.2 s | 0.84 s |
| 15 | 13.8 MiB | 14.3 | 27.5 s | 0.86 s |
| 19 | 11.2 MiB | 17.7 | 287.3 s | 0.92 s |

Measure on the target hardware with `python benchmarks/zstd_levels.py`
before fixing a level. Envelope `ZstdProfile` levels follow the same curve.

### Raw payloads

`pyhdiff.raw` exposes `hdiff_encode`, `hdiff_apply`, `zstd_encode` and
`zstd_apply` for callers that frame and verify their own data. These
functions neither hash nor frame. The apply functions need the exact target
length, and identifying the base becomes the caller's responsibility. The
payload bytes are the same ones an envelope carries.

### Threads, time and memory

Every encode, apply, compress and decompress call releases the GIL for its
whole native duration, and a test asserts this for each entry point. Calls
from several threads run in parallel, and they share no mutable state.

An encode cannot be interrupted. HDiffPatch matching has CPU-bound phases
without cancellation points, and a thread inside `encode_delta` finishes only
when the encode does. Bound the cost before the call: refuse inputs larger
than the budget allows. Encode time grows with input size and dissimilarity,
while apply and decompress are fast.

| Limit | Value |
| --- | ---: |
| Base, target, reconstruction (`RAW_MAX`) | 256 MiB |
| Payload inside an envelope (`PAYLOAD_MAX`) | 512 MiB − 96 B |
| `compress` input and `decompress` output (`FRAME_MAX`) | 1 GiB |
| HDiffPatch decode window | 2^23 B |
| zstd decode window, envelopes | 2^30 B |

Encoding holds the inputs, suffix structures and output in memory. Plan for
several times the input size on large HDiffPatch deltas.

## Envelope

All integers are unsigned big-endian. The header is exactly 96 bytes.

| Offset | Size | Field |
| ---: | ---: | --- |
| 0 | 4 | Magic, ASCII `PHDF` |
| 4 | 1 | Format version, `1` |
| 5 | 1 | Codec: `1` HDiffPatch single compressed diff with zstd streams; `2` one zstd frame, with the base as prefix |
| 6 | 2 | Reserved, zero |
| 8 | 8 | Base length |
| 16 | 8 | Target length |
| 24 | 8 | Payload length, exactly the bytes after the header |
| 32 | 32 | SHA-256 of the base |
| 64 | 32 | SHA-256 of the target |
| 96 | variable | Payload |

A codec 2 envelope with base length 0 is a standalone zstd frame. The
decoder rejects an unknown magic, version or codec, nonzero reserved bytes,
trailing or missing bytes, extra frames, and windows beyond the limits. The
hashes detect corruption and mismatched bases. They do not authenticate the
sender: anyone can produce a valid envelope.

## Compatibility

**Guarantee.** Every release of pyhdiff applies every envelope, raw payload
and zstd frame that any earlier release produced, and reconstructs the same
bytes. This holds for ever, across all future versions.

- `apply`, `inspect`, `decompress`, `raw.hdiff_apply` and `raw.zstd_apply`
  keep reading every format version, codec and parameter combination that
  was ever emitted. Limits that decoding enforces never shrink.
- Encoding may change between releases. A new release may emit different
  bytes for the same input, for example after a codec upgrade, as long as
  every earlier release's output still applies.
- A format change bumps the format version. No release reuses a version
  number or magic for different content.

**Enforcement.** `tests/golden/` holds, for every release, the envelopes and
frames that the release's own wheel produced from fixed inputs. CI applies
all of them at every commit. The release workflow refuses to run while any
earlier release's corpus is missing. A build that cannot read an older entry
fails its tests, and a failed test blocks the release, with no override.
Corpus entries are never edited or deleted.

**Reproducibility.** Release wheels are built twice, from scratch, in
digest-pinned containers with hash-pinned tools, and the release fails
unless both builds are bit-identical. Each release publishes the SHA-256 of
every asset.

## Building

The build requires the pinned toolchain, and it fails fast with any other
compiler. `scripts/toolchain.py` downloads the official LLVM 23.1.1 release
archive, checks its SHA-256, and builds private static libc++, libc++abi and
libunwind with extensive hardening. Under `PREFIX`, nothing is installed
system-wide.

```sh
scripts/container-setup.sh build             # inside python:3.12-slim-bookworm
python scripts/toolchain.py --prefix /opt/pyhdiff-toolchain
python scripts/native_checks.py --toolchain /opt/pyhdiff-toolchain
python scripts/wheel.py --toolchain /opt/pyhdiff-toolchain --out dist
```

`.github/workflows/build.yml` is the reference for the exact steps. The
native checks run the fault-injection suite, the fork's regression tests and
seven libc++ hardening traps. `--sanitize` repeats them under ASan and UBSan.

Hardening in the shipped extension:

- The C bridge is the only boundary into the C++ code. Every entry point is
  `noexcept` and turns any exception into a status code.
- Static libc++ is hardened in extensive mode, in a private ABI namespace,
  with bounded iterators and bounded `unique_ptr` arrays. Hardening failures
  terminate the process, even with `NDEBUG`.
- Code is built with `_FORTIFY_SOURCE=3`, strong stack protection, stack clash
  protection and zero-initialized locals. The bridge compiles with
  unsafe-buffer and lifetime diagnostics as errors.
- Linking uses full RELRO, immediate binding and a non-executable stack. All
  symbols are hidden except `PyInit__native`, unreferenced sections are
  discarded, and the binary is stripped. The libc++abi terminate handler does
  not demangle, which drops the demangler from the binary.
- The private libc++ resolves entirely inside the extension. A test loads a
  libstdc++ module globally before and after pyhdiff and throws through both
  runtimes.

Each release also publishes `pyhdiff-X.Y.Z-….debug`, the debug information
stripped from the extension, matched to it by GNU build ID and debuglink. It
is reproducible like the wheel and only needed to symbolize a crash.

## Pinned sources

| Component | Version | Pin |
| --- | --- | --- |
| HDiffPatch, [komastudios fork](https://github.com/komastudios/HDiffPatch) | 5.1.3 with UBSan fixes | commit `091e20cdc659c9547857c0e7d2ef3306701eecd3` |
| zstd | 1.5.7 | release archive, SHA-256 `eb33e51f49a15e023950cd7825ca74a4a2b43db8354825ac24fc1b7ee09e6fa3` |
| LLVM compiler | 23.1.1 | `LLVM-23.1.1-Linux-X64.tar.xz`, SHA-256 `832aeb58d105de1cabc7b982dd2c65de0610f7377df48ae8fc2dd8e97420a15c` |
| LLVM runtimes | 23.1.1 | `llvm-project-23.1.1.src.tar.xz`, SHA-256 `ebe9be46fe8756d58c5b198ffad0fa2a766257add81a4dc52179bfacc7888ee6` |

The wheel carries the license texts of all statically linked components in
`pyhdiff/licenses/`. pyhdiff itself is MIT licensed.
