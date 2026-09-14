#!/usr/bin/env python3
"""Build and run the native fault-injection, hardening and sanitizer checks.

    scripts/native_checks.py --toolchain PREFIX [--sanitize]

--sanitize builds with ASan and UBSan against the debug-hardened runtime,
enables leak detection, halts on the first finding and suppresses nothing.
"""

import argparse
import os
import resource
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--toolchain", type=Path, required=True)
    p.add_argument("--sanitize", action="store_true")
    p.add_argument("--jobs", type=int, default=os.cpu_count())
    a = p.parse_args()
    toolchain = a.toolchain.resolve()
    build = ROOT / "build" / ("native-sanitize" if a.sanitize else "native")
    env = dict(os.environ, PYHDIFF_TOOLCHAIN=str(toolchain))
    if a.sanitize:
        env["PYHDIFF_RUNTIME"] = str(toolchain / "runtime-debug")
    subprocess.run([
        "cmake", "-S", ROOT, "-B", build, "-G", "Ninja",
        f"-DCMAKE_TOOLCHAIN_FILE={ROOT / 'cmake/toolchain.cmake'}",
        f"-DCMAKE_BUILD_TYPE={'Debug' if a.sanitize else 'Release'}",
        "-DPYHDIFF_NATIVE_TESTS=ON",
        f"-DPYHDIFF_SANITIZE={'ON' if a.sanitize else 'OFF'}",
        f"-DPYHDIFF_HARDENING={'debug' if a.sanitize else 'extensive'}",
    ], check=True, env=env)
    subprocess.run(["cmake", "--build", build, "-j", str(a.jobs)], check=True, env=env)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    run_env = dict(env, ASAN_OPTIONS="detect_leaks=1:halt_on_error=1",
                   UBSAN_OPTIONS="halt_on_error=1:print_stacktrace=1")
    for binary, success in (("native_test", "native tests passed"),
                            ("hdiff_regression", "UBSan regressions passed")):
        result = subprocess.run([build / binary], env=run_env, capture_output=True, text=True)
        print(result.stdout + result.stderr)
        assert result.returncode == 0, f"{binary} exited {result.returncode}"
        assert success in result.stdout
        assert not result.stderr, "unexpected diagnostics"
    for kind in range(7):
        trap = subprocess.run([build / "hardening_test", str(kind)], env=run_env, capture_output=True)
        assert trap.returncode < 0, f"hardening probe {kind} did not trap: {trap.returncode}"
        assert b"AddressSanitizer" not in trap.stderr and b"runtime error:" not in trap.stderr
    print("seven hardening probes trapped")


if __name__ == "__main__":
    main()
