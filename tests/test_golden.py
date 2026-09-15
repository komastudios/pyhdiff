"""HEAD must apply every delta that any released version produced, and must
encode the corpus inputs to exactly the bytes the newest release produced."""

import hashlib
import json
import sys
from pathlib import Path

import pytest

import pyhdiff

GOLDEN = Path(__file__).parent / "golden"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import golden  # noqa: E402


def releases():
    def version(path):
        return tuple(int(part) for part in path.name.removeprefix("v").split("."))
    return sorted((m.parent for m in GOLDEN.glob("v*/manifest.json")), key=version)


def cases():
    for release in releases():
        data = json.loads((release / "manifest.json").read_text())
        for entry in data["entries"]:
            yield pytest.param(release, entry, id=f"{release.name}-{entry['name']}")


@pytest.mark.parametrize(("release", "entry"), list(cases()))
def test_released_blob_applies(release, entry, vector_inputs):
    blob = (release / entry["blob"]).read_bytes()
    assert hashlib.sha256(blob).hexdigest() == entry["blob_sha256"]
    base = vector_inputs[entry["base_file"]] if entry["base_file"] else b""
    target = vector_inputs[entry["target_file"]]
    kind = entry["kind"]
    if kind == "frame":
        assert pyhdiff.decompress(blob, len(target)) == target
    else:
        assert pyhdiff.apply(base, blob) == target


def test_encoding_matches_newest_release():
    # Encoded bytes may change only as a deliberate, documented decision: such
    # a change edits this test in the same commit and says so in CHANGELOG.md.
    if not releases():
        pytest.skip("no released corpus yet")
    newest = releases()[-1]
    recorded = {e["blob"]: e for e in json.loads((newest / "manifest.json").read_text())["entries"]}
    encoded = {name: (kind, base_file, target_file, hashlib.sha256(blob).hexdigest())
               for name, kind, base_file, target_file, blob in golden.encode_corpus()}
    assert encoded.keys() == recorded.keys()
    differing = [name for name, (kind, base_file, target_file, digest) in encoded.items()
                 if (kind, base_file, target_file, digest)
                 != tuple(recorded[name][k] for k in ("kind", "base_file", "target_file", "blob_sha256"))]
    assert not differing, f"HEAD encodes differently from {newest.name}: {differing}"
