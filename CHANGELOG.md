# Changelog

## [Unreleased]

- CI checks encoding identity as well as decoding: the golden corpus inputs
  must encode to exactly the bytes of the newest release's corpus.

## [0.1.1]

- A native allocation failure raises `pyhdiff.AllocationError`, a subclass of
  both `pyhdiff.Error` and `MemoryError`, instead of a plain `MemoryError`, so
  that catching `pyhdiff.Error` covers every codec failure.
- README: the exception hierarchy is documented exactly; `pyhdiff.Error` is not
  a `ValueError`.

## [0.1.0]

First release.

- Envelope format version 1 (`PHDF`): HDiffPatch deltas (codec 1) and zstd
  frames with an optional prefix base (codec 2), with base and target lengths
  and SHA-256 hashes verified on apply.
- `encode_base`, `encode_delta`, `apply`, `inspect`; `pyhdiff.raw` payload
  codecs; standard zstd `compress` and bounded `decompress`.
- Default profiles reproduce the reference encoder's payloads byte for byte
  (`tests/vectors`).
- The GIL is released around every codec call.
- Self-contained cp312-abi3 manylinux_2_34 x86-64 wheel: statically linked
  HDiffPatch 5.1.3 (komastudios fork 091e20cd), zstd 1.5.7 and hardened
  libc++ from LLVM 23.1.1.
