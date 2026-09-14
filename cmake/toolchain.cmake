# Selects the pinned compiler and private runtime from scripts/toolchain.py.
# Point PYHDIFF_TOOLCHAIN at its prefix; PYHDIFF_RUNTIME may select another
# runtime below it, such as runtime-debug for sanitizer builds.
if(NOT DEFINED ENV{PYHDIFF_TOOLCHAIN})
  message(FATAL_ERROR "Set PYHDIFF_TOOLCHAIN to the prefix written by scripts/toolchain.py")
endif()
set(CMAKE_C_COMPILER "$ENV{PYHDIFF_TOOLCHAIN}/llvm/bin/clang")
set(CMAKE_CXX_COMPILER "$ENV{PYHDIFF_TOOLCHAIN}/llvm/bin/clang++")
if(DEFINED ENV{PYHDIFF_RUNTIME})
  set(PYHDIFF_RUNTIME "$ENV{PYHDIFF_RUNTIME}" CACHE PATH "")
else()
  set(PYHDIFF_RUNTIME "$ENV{PYHDIFF_TOOLCHAIN}/runtime" CACHE PATH "")
endif()
