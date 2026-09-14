# Verification

What each release is checked against, and where. Every check runs in CI on
GitHub-hosted runners from pinned inputs; the release workflow runs all of
them for the tagged commit and publishes nothing if one fails. All test data
is synthetic.

## Native bridge

`scripts/native_checks.py`, in `build.yml`.

| Check | Cases |
| --- | --- |
| HDiffPatch round trips | empty base and target, empty base, empty target, sparse edits, identical, random 512 B target |
| Truncation | every prefix of every encoded payload fails cleanly, HDiffPatch and zstd |
| Mutation | every byte of every payload flipped: decodes to the stated size or fails; never crashes |
| Size mismatch | target one byte short or long; trailing byte; second frame |
| Option ranges | each HDiffPatch and zstd parameter just outside its range; LDM rules; null pointers |
| Parameter wiring | every zstd parameter read back after assignment (test build) |
| Exception containment | `std::bad_alloc`, `std::exception`, unknown exceptions thrown inside codec callbacks and zstd allocation failure, 100 repetitions each, all turned into status codes without allocation |
| Frames | multi-frame, skippable frames, hard output bound, truncation, bit-flip sweep, trailing garbage, empty frame |
| Allocation failure | a frame claiming 512 MiB under a 256 MiB address-space limit fails with an allocation status and owns nothing |
| HDiffPatch fork regressions | the fork's UBSan regression suite |
| Hardening | seven libc++ out-of-bounds probes (vector, iterators of vector, string, array, span, string_view, `unique_ptr<T[]>`) must terminate with `NDEBUG` |
| Sanitizers (CI) | the native suite and fork regressions under ASan, UBSan and leak detection, halting on the first finding, no suppressions, with debug-hardened libc++ |

## Extension binary

`tests/test_elf.py`, in `build.yml`, against the installed wheel.

- The only exported symbol is `PyInit__native`; no symbol table and no debug
  sections remain.
- Dynamic dependencies are glibc libraries only; no RPATH or RUNPATH.
- Full RELRO, `BIND_NOW` and a non-executable stack.
- No undefined C++ runtime or unwinder symbols: the private libc++ is fully
  resolved inside the extension and cannot interpose with another C++ runtime
  in the process.
- `auditwheel show` confirms the manylinux_2_34 policy (`scripts/wheel.py`).
- Runtime coexistence (`tests/test_mixed_runtime.py`): a libstdc++ module
  loaded with `RTLD_GLOBAL` before and after pyhdiff; both runtimes throw and
  catch while the other is live.
- The stripped extension carries a build ID and debuglink to the published
  `.debug` file; neither contains build-machine paths.

## Python package

`tests/`, in `test.yml`, against the installed wheel on
`python:3.12-slim-trixie` and `python:3.13-slim-trixie`.

| Check | File |
| --- | --- |
| Reference bytes: 63 vectors (9 input pairs up to 12 MiB, 7 profiles) reproduce the reference encoder's envelope bytes 4..end and payload exactly, and apply | `test_vectors.py` |
| Envelope layout, inspect, every truncation, extension, mutated header fields, wrong base by length and by hash, tampered target hash and length, output limit, payload mutations, option errors, input types | `test_envelope.py` |
| zstd frames: levels 1–22, hard bound with and without content size, forged content size above and just below the bound (no memory committed), content size smaller and larger than the data, legacy magic, dictionary frames, exact bound followed by skippable and empty frames, concatenation, corruption, zstd CLI interoperability in both directions | `test_frames.py` |
| GIL released by every native entry point, checked deterministically without timing: with an effectively infinite switch interval a helper thread can only advance while the GIL is explicitly released; a control confirms a GIL-holding C call leaves it exactly unchanged | `test_gil.py` |
| Parallel encodes from eight threads equal serial results | `test_concurrency.py` |
| Every earlier release's golden corpus applies | `test_golden.py` |

## Reproducibility

`scripts/wheel.py` builds without build isolation from hash-pinned tools in a
digest-pinned image, with `SOURCE_DATE_EPOCH` set to the commit time and all
build paths mapped out of the binary. The release workflow builds the
toolchain and wheel twice in separate jobs from scratch and fails unless the
two wheels, and the two debug files, are identical.
