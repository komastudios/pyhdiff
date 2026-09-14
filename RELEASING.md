# Releasing

Releases are cut by `.github/workflows/release.yml` for an explicitly chosen
commit. Nothing publishes automatically from `main`.

1. Make sure `tests/golden/` has a directory for every earlier release tag.
   If the last release's corpus is not committed yet:

   ```sh
   python scripts/golden.py import vX.Y.Z   # verifies the asset against SHA256SUMS
   git add tests/golden/vX.Y.Z && git commit -m "Add golden corpus of vX.Y.Z"
   ```

2. Set `version` in `pyproject.toml`, add the `CHANGELOG.md` entry, merge to
   `main`, and wait for CI to pass.

3. Optionally, run the release workflow by hand with `dry_run` enabled. It
   builds, tests and packages everything but does not upload.

4. Tag and push:

   ```sh
   git tag -a vX.Y.Z -m "pyhdiff X.Y.Z" && git push origin vX.Y.Z
   ```

The workflow refuses to publish if the tag and `pyproject.toml` disagree, if
an earlier release's golden corpus is missing, if any test fails (including
applying every earlier corpus), or if two independent builds of the wheel
differ. There is no override: fix the cause and tag a new version.

After publishing, import and commit the new release's corpus (step 1), so that
every later commit is tested against it.

Never delete a published release or its assets; consumers pin their URLs and
digests.
