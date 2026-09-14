#!/usr/bin/env python3
"""Install the pinned LLVM compiler and build private, hardened LLVM runtimes.

Everything lands under one prefix and nothing is installed system-wide:

    PREFIX/llvm           clang 23.1.1 from the official release archive
    PREFIX/runtime        static libc++, libc++abi, libunwind, compiler-rt
    PREFIX/runtime-debug  the same with debug hardening (--mode debug)

Downloads are verified by SHA-256 before use.
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import urllib.request
from pathlib import Path

LLVM_VERSION = "23.1.1"
LLVM_REV = "6dfe1677ab8dffbc6ec13d53a1e0215d75147689"
RELEASE = f"https://github.com/llvm/llvm-project/releases/download/llvmorg-{LLVM_VERSION}"
COMPILER = (
    f"LLVM-{LLVM_VERSION}-Linux-X64.tar.xz",
    "832aeb58d105de1cabc7b982dd2c65de0610f7377df48ae8fc2dd8e97420a15c",
)
SOURCE = (
    f"llvm-project-{LLVM_VERSION}.src.tar.xz",
    "ebe9be46fe8756d58c5b198ffad0fa2a766257add81a4dc52179bfacc7888ee6",
)
SOURCE_DIRS = ["cmake", "compiler-rt", "libc", "libcxx", "libcxxabi", "libunwind",
               "runtimes", "llvm/cmake", "llvm/include", "llvm/utils"]
ABI_NAMESPACE = "__phd23"
ABI = [
    "_LIBCPP_ABI_BOUNDED_ITERATORS",
    "_LIBCPP_ABI_BOUNDED_ITERATORS_IN_VECTOR",
    "_LIBCPP_ABI_BOUNDED_ITERATORS_IN_STRING",
    "_LIBCPP_ABI_BOUNDED_ITERATORS_IN_STD_ARRAY",
    "_LIBCPP_ABI_BOUNDED_UNIQUE_PTR",
]
HARDEN = (
    "-O2 -D_FORTIFY_SOURCE=3 -fstack-protector-strong "
    "-fstack-clash-protection -ftrivial-auto-var-init=zero"
)

COMMANDS = []


def run(*args, **kw):
    COMMANDS.append(list(map(str, args)))
    subprocess.run(list(map(str, args)), check=True, **kw)


def digest(path):
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def fetch(name, sha256, cache):
    path = cache / name
    if not path.exists():
        cache.mkdir(parents=True, exist_ok=True)
        partial = path.with_suffix(path.suffix + ".partial")
        with urllib.request.urlopen(f"{RELEASE}/{name}") as response, partial.open("wb") as out:
            shutil.copyfileobj(response, out, 1 << 20)
        partial.rename(path)
    if digest(path) != sha256:
        raise SystemExit(f"SHA-256 mismatch: {path}")
    return path


def install_compiler(archive, prefix):
    llvm = prefix / "llvm"
    if (llvm / "bin/clang-23").exists():
        return llvm
    top = archive.name.removesuffix(".tar.xz")
    staging = prefix / "llvm.partial"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    # Only the driver and its resource directory; the full archive is 12 GiB.
    run("tar", "-xJf", archive, "-C", staging, "--strip-components=1",
        f"{top}/bin/clang", f"{top}/bin/clang++", f"{top}/bin/clang-23",
        f"{top}/lib/clang")
    staging.rename(llvm)
    return llvm


def build_runtime(a, llvm, source, prefix, mode):
    out = prefix / ("runtime" if mode == "extensive" else "runtime-debug")
    if (out / "provenance.json").exists():
        return out
    work = Path(tempfile.mkdtemp(prefix="phd-runtime-"))
    staging = prefix / (out.name + ".partial")
    shutil.rmtree(staging, ignore_errors=True)
    # Build paths vary between machines; keep them out of the archives.
    flags = f"{HARDEN} -ffile-prefix-map={source}=llvm-project -ffile-prefix-map={work}=build"
    common = [
        "-G", "Ninja",
        "-DCMAKE_BUILD_TYPE=Release",
        "-DCMAKE_POSITION_INDEPENDENT_CODE=ON",
        "-DCMAKE_C_COMPILER_TARGET=x86_64-unknown-linux-gnu",
        "-DCMAKE_CXX_COMPILER_TARGET=x86_64-unknown-linux-gnu",
        f"-DCMAKE_C_COMPILER={llvm}/bin/clang",
        f"-DCMAKE_CXX_COMPILER={llvm}/bin/clang++",
        f"-DCMAKE_INSTALL_PREFIX={staging}",
        f"-DCMAKE_C_FLAGS={flags}",
        f"-DCMAKE_CXX_FLAGS={flags}",
        "-DLLVM_ENABLE_PER_TARGET_RUNTIME_DIR=OFF",
    ]
    builtins = work / "builtins"
    run("cmake", "-S", source / "compiler-rt/lib/builtins", "-B", builtins, *common,
        "-DCOMPILER_RT_DEFAULT_TARGET_ONLY=ON", "-DCOMPILER_RT_BUILTINS_HIDE_SYMBOLS=ON")
    run("cmake", "--build", builtins, "-j", a.jobs)
    run("cmake", "--install", builtins)
    runtimes = work / "runtimes"
    run(
        "cmake", "-S", source / "runtimes", "-B", runtimes, *common,
        "-DLLVM_INCLUDE_TESTS=OFF",
        "-DLLVM_ENABLE_RUNTIMES=libcxx;libcxxabi;libunwind",
        f"-DLIBCXX_HARDENING_MODE={mode}",
        f"-DLIBCXX_ABI_NAMESPACE={ABI_NAMESPACE}",
        "-DLIBCXX_ABI_DEFINES=" + ";".join(ABI),
        "-DLIBCXX_ENABLE_SHARED=OFF",
        "-DLIBCXX_ENABLE_STATIC=ON",
        "-DLIBCXX_HERMETIC_STATIC_LIBRARY=ON",
        "-DLIBCXX_ENABLE_EXCEPTIONS=ON",
        "-DLIBCXX_ENABLE_RTTI=ON",
        "-DLIBCXX_INCLUDE_TESTS=OFF",
        "-DLIBCXX_INCLUDE_BENCHMARKS=OFF",
        "-DLIBCXX_ENABLE_TIME_ZONE_DATABASE=OFF",
        "-DLIBCXXABI_ENABLE_SHARED=OFF",
        "-DLIBCXXABI_ENABLE_STATIC=ON",
        "-DLIBCXXABI_HERMETIC_STATIC_LIBRARY=ON",
        "-DLIBCXXABI_USE_LLVM_UNWINDER=ON",
        "-DLIBCXXABI_ENABLE_STATIC_UNWINDER=ON",
        "-DLIBCXXABI_INCLUDE_TESTS=OFF",
        # The default terminate handler is the only user of the 140 KiB demangler;
        # the bridge catches every exception, so terminate never names a type.
        "-DLIBCXXABI_NON_DEMANGLING_TERMINATE=ON",
        "-DLIBUNWIND_ENABLE_SHARED=OFF",
        "-DLIBUNWIND_ENABLE_STATIC=ON",
        "-DLIBUNWIND_HIDE_SYMBOLS=ON",
        "-DLIBUNWIND_INCLUDE_TESTS=OFF",
    )
    run("cmake", "--build", runtimes, "-j", a.jobs)
    run("cmake", "--install", runtimes)
    licenses = staging / "licenses"
    licenses.mkdir(exist_ok=True)
    for project in ("libcxx", "libcxxabi", "libunwind", "compiler-rt"):
        shutil.copyfile(source / project / "LICENSE.TXT", licenses / f"llvm-{project}.txt")
    files = sorted(
        f for f in (*staging.glob("licenses/*"), *staging.glob("lib/**/*.a"),
                    *staging.glob("include/**/*"))
        if f.is_file()
    )
    (staging / "provenance.json").write_text(json.dumps({
        "llvm_version": LLVM_VERSION,
        "llvm_revision": LLVM_REV,
        "compiler_archive": dict(zip(("name", "sha256"), COMPILER)),
        "source_archive": dict(zip(("name", "sha256"), SOURCE)),
        "mode": mode,
        "abi_defines": ABI,
        "non_demangling_terminate": True,
        "namespace": ABI_NAMESPACE,
        "flags": HARDEN,
        "sha256": {str(f.relative_to(staging)): digest(f) for f in files},
    }, indent=2) + "\n")
    shutil.rmtree(work)
    staging.rename(out)
    return out


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--prefix", type=Path, required=True)
    p.add_argument("--cache", type=Path, help="download cache (default PREFIX/downloads)")
    p.add_argument("--mode", choices=["extensive", "debug"], action="append",
                   help="runtime hardening mode; repeatable (default extensive)")
    p.add_argument("--jobs", type=int, default=os.cpu_count())
    a = p.parse_args()
    prefix = a.prefix.resolve()
    cache = (a.cache or prefix / "downloads").resolve()
    llvm = install_compiler(fetch(*COMPILER, cache), prefix)
    version = subprocess.check_output([llvm / "bin/clang", "--version"], text=True)
    if f"clang version {LLVM_VERSION} " not in version or LLVM_REV not in version:
        raise SystemExit(f"unexpected compiler: {version}")
    source = prefix / "llvm-project"
    if not source.exists():
        archive = fetch(*SOURCE, cache)
        top = archive.name.removesuffix(".tar.xz")
        staging = prefix / "llvm-project.partial"
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir()
        run("tar", "-xJf", archive, "-C", staging, "--strip-components=1",
            *(f"{top}/{d}" for d in SOURCE_DIRS))
        staging.rename(source)
    for mode in a.mode or ["extensive"]:
        print(build_runtime(a, llvm, source, prefix, mode))


if __name__ == "__main__":
    main()
