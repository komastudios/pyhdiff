"""Byte identity with the reference envelopes of the measured C bridge."""

import hashlib
import json
from pathlib import Path

import pytest

import pyhdiff


def profile(spec):
    params = dict(spec["params"])
    level = params.pop("compression_level")
    if spec["codec_id"] == 1:
        return pyhdiff.HDiffProfile(level=level, **params)
    return pyhdiff.ZstdProfile(level=level, **params)


def cases():
    manifest = json.loads((Path(__file__).parent / "vectors/manifest.json").read_text())
    for v in manifest["vectors"]:
        marks = [pytest.mark.slow] if v["pair"].startswith("large-") else []
        yield pytest.param(v, manifest["profiles"][v["profile"]], id=f"{v['pair']}-{v['profile']}",
                           marks=marks)


@pytest.mark.parametrize(("vector", "spec"), list(cases()))
def test_reference_bytes(vector, spec, vector_inputs):
    base = vector_inputs[vector["base_file"]] if vector["base_file"] else b""
    target = vector_inputs[vector["target_file"]]
    if spec["empty_base"]:
        assert base == b""
    if spec["codec_id"] == 1:
        blob = pyhdiff.encode_delta(base, target, profile(spec))
    elif spec["empty_base"]:
        blob = pyhdiff.encode_base(target, profile(spec))
    else:
        blob = pyhdiff.encode_delta(base, target, profile(spec))
    assert blob[:4] == pyhdiff.MAGIC
    assert hashlib.sha256(blob[4:]).hexdigest() == vector["envelope_bytes_4_end_sha256"]
    assert hashlib.sha256(blob[96:]).hexdigest() == vector["payload_sha256"]
    header = pyhdiff.inspect(blob)
    assert header.codec == vector["codec_id"]
    assert header.payload_length == vector["payload_bytes"]
    assert pyhdiff.apply(base, blob) == target
