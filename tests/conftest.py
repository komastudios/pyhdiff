import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

VECTORS = Path(__file__).parent / "vectors"


@pytest.fixture(scope="session")
def vector_manifest():
    return json.loads((VECTORS / "manifest.json").read_text())


@pytest.fixture(scope="session")
def vector_inputs(tmp_path_factory, vector_manifest):
    """Map file name to bytes, regenerating the large pairs that are not committed."""
    spec = importlib.util.spec_from_file_location("gen_inputs", VECTORS / "gen_inputs.py")
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    files = {}
    for label, base, target in generator.pairs():
        for suffix, data in (("base", base), ("target", target)):
            name = f"{label}.{suffix}"
            committed = VECTORS / name
            files[name] = committed.read_bytes() if committed.exists() else data
    expected = {}
    for v in vector_manifest["vectors"]:
        if v["base_file"]:
            expected[v["base_file"]] = v["base_sha256"]
        expected[v["target_file"]] = v["target_sha256"]
    for name, digest in expected.items():
        assert hashlib.sha256(files[name]).hexdigest() == digest, f"input {name} does not reproduce"
    return files
