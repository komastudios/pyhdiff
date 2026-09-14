# Golden corpus

One directory per released tag, holding the envelopes and zstd frames that
release produced from the inputs in `tests/vectors`. `tests/test_golden.py`
applies every one of them with the code under test. A release cannot be cut
while an earlier tag's directory is missing, and a release whose build cannot
read an older entry fails. Entries are never edited or removed.

Add a released corpus with `scripts/golden.py import vX.Y.Z`, which downloads
the `pyhdiff-X.Y.Z-golden.tar.gz` release asset and checks it against the
release's `SHA256SUMS` before unpacking.
