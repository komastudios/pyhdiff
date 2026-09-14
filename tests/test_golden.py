"""HEAD must apply every delta that any released version produced."""

import hashlib
import json
from pathlib import Path

import pytest

import pyhdiff

GOLDEN = Path(__file__).parent / "golden"


def cases():
    for manifest in sorted(GOLDEN.glob("v*/manifest.json")):
        data = json.loads(manifest.read_text())
        for entry in data["entries"]:
            yield pytest.param(manifest.parent, entry, id=f"{manifest.parent.name}-{entry['name']}")


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
