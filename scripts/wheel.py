#!/usr/bin/env python3
"""Build the release wheel reproducibly, audit it and print its SHA-256.

    scripts/wheel.py --toolchain PREFIX [--out dist]

The wheel is built without build isolation from the hash-pinned tools in
requirements/build.txt. SOURCE_DATE_EPOCH is the commit time, so two builds
of one commit with one toolchain produce identical bytes.
"""

import argparse
import base64
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLATFORM = "manylinux_2_34_x86_64"


def policy(wheel, env):
    """The newest glibc policy auditwheel finds the wheel consistent with."""
    shown = subprocess.run([sys.executable, "-m", "auditwheel", "show", wheel], check=True,
                           capture_output=True, text=True, env=env).stdout
    print(shown)
    match = re.search(r'consistent with the following platform tag:\s*"manylinux_2_(\d+)_x86_64"',
                      " ".join(shown.split()))
    if not match:
        raise SystemExit("auditwheel did not confirm a manylinux policy")
    return int(match.group(1))


def retag(built, out):
    """Rewrite the platform tag in place of auditwheel repair, which has nothing to graft.

    Entry order, timestamps and compression stay as built, so the result is as
    reproducible as the input.
    """
    name = built.name.replace("-linux_x86_64.whl", f"-{PLATFORM}.whl")
    with zipfile.ZipFile(built) as source:
        entries = [(info, source.read(info)) for info in source.infolist()]
    wheel_meta = next(i.filename for i, _ in entries if i.filename.endswith(".dist-info/WHEEL"))
    record = next(i.filename for i, _ in entries if i.filename.endswith(".dist-info/RECORD"))
    rewritten = []
    for info, data in entries:
        if info.filename == wheel_meta:
            text = data.decode()
            if text.count("Tag: cp312-abi3-linux_x86_64\n") != 1:
                raise SystemExit(f"unexpected WHEEL tags: {text}")
            data = text.replace("cp312-abi3-linux_x86_64", f"cp312-abi3-{PLATFORM}").encode()
            digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
            meta_line = f"{wheel_meta},sha256={digest},{len(data)}"
        rewritten.append((info, data))
    final = []
    for info, data in rewritten:
        if info.filename == record:
            lines = [meta_line if line.startswith(f"{wheel_meta},") else line
                     for line in data.decode().splitlines()]
            data = ("\n".join(lines) + "\n").encode()
        final.append((info, data))
    with zipfile.ZipFile(out / name, "w") as target:
        for info, data in final:
            target.writestr(info, data, compress_type=info.compress_type)
    return out / name


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--toolchain", type=Path, required=True)
    p.add_argument("--out", type=Path, default=ROOT / "dist")
    a = p.parse_args()
    env = dict(os.environ, PYHDIFF_TOOLCHAIN=str(a.toolchain.resolve()),
               GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="safe.directory", GIT_CONFIG_VALUE_0="*")
    if "SOURCE_DATE_EPOCH" not in env:
        env["SOURCE_DATE_EPOCH"] = subprocess.check_output(
            ["git", "-C", ROOT, "log", "-1", "--format=%ct"], text=True, env=env).strip()
    shutil.rmtree(ROOT / "build", ignore_errors=True)
    a.out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as raw:
        subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-build-isolation", "--no-deps",
                        "--wheel-dir", raw, ROOT], check=True, env=env)
        (built,) = Path(raw).glob("*.whl")
        if policy(built, env) > 34:
            raise SystemExit("the extension needs a newer glibc than manylinux_2_34 allows")
        for stale in (*a.out.glob("pyhdiff-*.whl"), *a.out.glob("pyhdiff-*.debug")):
            stale.unlink()
        retag(built, a.out)
    (wheel,) = a.out.glob(f"pyhdiff-*-cp312-abi3-{PLATFORM}.whl")
    (debug,) = (ROOT / "build").glob("*/_native.debug")
    shutil.copyfile(debug, a.out / wheel.name.replace(".whl", ".debug"))
    for path in (a.out / wheel.name.replace(".whl", ".debug"), wheel):
        with path.open("rb") as f:
            print(f"{hashlib.file_digest(f, 'sha256').hexdigest()}  {path.name}")


if __name__ == "__main__":
    main()
