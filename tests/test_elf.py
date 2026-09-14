"""The extension exports only its initializer and links nothing but glibc."""

import os
import re
import shutil
import subprocess

import pytest

from pyhdiff import _native

readelf = shutil.which("readelf")
pytestmark = pytest.mark.skipif(
    not readelf and not os.environ.get("PYHDIFF_REQUIRE_ELF_AUDIT"), reason="binutils missing"
)
GLIBC_LIBS = {"libc.so.6", "libm.so.6", "libdl.so.2", "libpthread.so.0", "ld-linux-x86-64.so.2"}


def tool(*args):
    return subprocess.check_output([*args, _native.__file__], text=True)


def test_exports_only_the_initializer():
    assert readelf
    symbols = tool("nm", "-D", "--defined-only")
    assert {line.split()[-1] for line in symbols.splitlines()} == {"PyInit__native"}


def test_no_symbol_table_or_debug_info():
    sections = tool("readelf", "-SW")
    assert ".symtab" not in sections
    assert ".debug_" not in sections


def test_dynamic_dependencies_and_hardening():
    dynamic = tool("readelf", "-dW")
    assert set(re.findall(r"Shared library: \[(.*?)\]", dynamic)) <= GLIBC_LIBS
    assert "BIND_NOW" in dynamic and "RPATH" not in dynamic and "RUNPATH" not in dynamic
    segments = tool("readelf", "-lW")
    assert "GNU_RELRO" in segments
    stack = next(line for line in segments.splitlines() if "GNU_STACK" in line)
    assert "RWE" not in stack


def test_no_cxx_runtime_leaks():
    undefined = tool("nm", "-D", "--undefined-only").replace("__cxa_finalize", "")
    assert not re.search(r"\b(_Z\w+|__cxa_\w+|_Unwind_\w+|__gxx\w+)", undefined), undefined
    python = {s for s in re.findall(r"\bU (\w+)", undefined) if s.startswith(("Py", "_Py"))}
    assert python, "expected references to the Python C API"
