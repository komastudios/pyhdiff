#!/usr/bin/env python3
"""Golden corpus: envelopes and frames produced by every released version.

    golden.py generate --out DIR          encode the corpus with the installed pyhdiff
    golden.py pack DIR ARCHIVE            deterministic tar.gz of a generated corpus
    golden.py import TAG                  fetch a release's corpus into tests/golden/TAG
    golden.py check-complete [--except TAG]
                                          every released tag must have a corpus

The release workflow generates and publishes the corpus of the version it
releases. After publishing, `import` commits it, and the next release refuses
to proceed until every earlier tag's corpus is present and applies at HEAD.
"""

import argparse
import gzip
import hashlib
import io
import json
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VECTORS = ROOT / "tests/vectors"
GOLDEN = ROOT / "tests/golden"
REPOSITORY = "https://github.com/komastudios/pyhdiff"
PAIRS = ["small-sparse-edits", "small-bulk-churn", "medium-sparse-edits", "medium-bulk-churn",
         "identical", "unrelated", "empty-base-and-small-target"]
PROFILES = ["hdiff-default", "hdiff-score4", "zstd-auto19", "zstd-prefix12", "zstd-standalone3"]
FRAME_LEVELS = [1, 3, 19]


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def inputs():
    # Corpus pairs are all committed; the large, regenerated pairs are not used.
    files = {}
    for pair in PAIRS:
        for suffix in ("base", "target"):
            files[f"{pair}.{suffix}"] = (VECTORS / f"{pair}.{suffix}").read_bytes()
    return files


def generate(out):
    import pyhdiff

    manifest = json.loads((VECTORS / "manifest.json").read_text())
    files = inputs()
    out.mkdir(parents=True, exist_ok=True)
    entries = []

    def record(name, kind, base_file, target_file, blob):
        (out / name).write_bytes(blob)
        entries.append({"name": name.rsplit(".", 1)[0], "kind": kind, "blob": name,
                        "blob_sha256": sha256(blob), "base_file": base_file,
                        "target_file": target_file,
                        "target_sha256": sha256(files[target_file])})

    for pair in PAIRS:
        base_file, target_file = f"{pair}.base", f"{pair}.target"
        base, target = files[base_file], files[target_file]
        for name in PROFILES:
            spec = manifest["profiles"][name]
            params = dict(spec["params"])
            level = params.pop("compression_level")
            if spec["codec_id"] == 1:
                blob = pyhdiff.encode_delta(base, target, pyhdiff.HDiffProfile(level=level, **params))
                used_base = base_file
            elif spec["empty_base"]:
                blob = pyhdiff.encode_base(target, pyhdiff.ZstdProfile(level=level, **params))
                used_base = ""
            else:
                blob = pyhdiff.encode_delta(base, target, pyhdiff.ZstdProfile(level=level, **params))
                used_base = base_file
            record(f"{pair}.{name}.phdf", "envelope", used_base, target_file, blob)
        if pair.startswith("medium"):
            for level in FRAME_LEVELS:
                record(f"{pair}.compress-{level}.zst", "frame", "", target_file,
                       pyhdiff.compress(target, level))
    (out / "manifest.json").write_text(json.dumps({
        "pyhdiff_version": pyhdiff.__version__,
        "format_version": pyhdiff.FORMAT_VERSION,
        "inputs": "tests/vectors",
        "entries": entries,
    }, indent=1) + "\n")


def pack(directory, archive):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for path in sorted(directory.iterdir()):
            info = tarfile.TarInfo(path.name)
            info.size = path.stat().st_size
            info.mode = 0o644
            with path.open("rb") as f:
                tar.addfile(info, f)
    with archive.open("wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0,
                                                   filename="") as gz:
        gz.write(buffer.getvalue())


def download(url):
    with urllib.request.urlopen(url) as response:
        return response.read()


def import_release(tag):
    version = tag.removeprefix("v")
    name = f"pyhdiff-{version}-golden.tar.gz"
    sums = download(f"{REPOSITORY}/releases/download/{tag}/SHA256SUMS").decode()
    expected = next(line.split()[0] for line in sums.splitlines() if line.endswith(f"  {name}"))
    archive = download(f"{REPOSITORY}/releases/download/{tag}/{name}")
    if sha256(archive) != expected:
        raise SystemExit(f"{name} does not match SHA256SUMS")
    target = GOLDEN / tag
    if target.exists():
        raise SystemExit(f"{target} exists")
    with tempfile.TemporaryDirectory() as tmp:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            tar.extractall(tmp, filter="data")
        manifest = json.loads((Path(tmp) / "manifest.json").read_text())
        if manifest["pyhdiff_version"] != version:
            raise SystemExit("corpus version does not match the tag")
        for entry in manifest["entries"]:
            if sha256((Path(tmp) / entry["blob"]).read_bytes()) != entry["blob_sha256"]:
                raise SystemExit(f"corrupt corpus entry {entry['blob']}")
        manifest["release_asset"] = {"name": name, "sha256": expected}
        target.mkdir(parents=True)
        for path in Path(tmp).iterdir():
            if path.name != "manifest.json":
                (target / path.name).write_bytes(path.read_bytes())
        (target / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n")
    print(f"imported {len(manifest['entries'])} entries into {target}")


def check_complete(exclude):
    tags = subprocess.check_output(["git", "-C", ROOT, "tag", "--list", "v*.*.*"],
                                   text=True).split()
    missing = [t for t in tags if t != exclude and not (GOLDEN / t / "manifest.json").exists()]
    if missing:
        raise SystemExit("golden corpus missing for released tags: " + ", ".join(missing)
                         + " (run scripts/golden.py import TAG and commit)")
    print(f"golden corpus present for {len(tags) - (exclude in tags)} released tag(s)")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    g = sub.add_parser("generate")
    g.add_argument("--out", type=Path, required=True)
    k = sub.add_parser("pack")
    k.add_argument("directory", type=Path)
    k.add_argument("archive", type=Path)
    i = sub.add_parser("import")
    i.add_argument("tag")
    c = sub.add_parser("check-complete")
    c.add_argument("--except", dest="exclude", default="")
    a = p.parse_args()
    if a.command == "generate":
        generate(a.out)
    elif a.command == "pack":
        pack(a.directory, a.archive)
    elif a.command == "import":
        import_release(a.tag)
    else:
        check_complete(a.exclude)


if __name__ == "__main__":
    sys.exit(main())
