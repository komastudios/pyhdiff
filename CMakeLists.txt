cmake_minimum_required(VERSION 3.30)
project(pyhdiff LANGUAGES C CXX)
set(CMAKE_C_STANDARD 11)
set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_SCAN_FOR_MODULES OFF)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)
if(NOT CMAKE_SYSTEM_NAME STREQUAL "Linux" OR NOT CMAKE_SYSTEM_PROCESSOR STREQUAL "x86_64")
  message(FATAL_ERROR "Only x86-64 Linux is supported")
endif()
if(NOT CMAKE_CXX_COMPILER_ID STREQUAL "Clang" OR NOT CMAKE_CXX_COMPILER_VERSION VERSION_EQUAL "23.1.1")
  message(FATAL_ERROR "LLVM 23.1.1 is required; see scripts/toolchain.py")
endif()
if(NOT DEFINED PYHDIFF_RUNTIME)
  message(FATAL_ERROR "Set PYHDIFF_RUNTIME to the private LLVM runtime prefix")
endif()

set(PYHDIFF_HARDENING extensive CACHE STRING "libc++ hardening mode")
if(NOT PYHDIFF_HARDENING MATCHES "^(extensive|debug)$")
  message(FATAL_ERROR "Unsupported hardening mode")
endif()
cmake_path(GET CMAKE_CXX_COMPILER PARENT_PATH llvm_bin)
cmake_path(GET llvm_bin PARENT_PATH llvm_root)
add_compile_options(-O2 -g -D_FORTIFY_SOURCE=3 -fstack-protector-strong -fstack-clash-protection
  -ftrivial-auto-var-init=zero -fvisibility=hidden -ffunction-sections -fdata-sections
  "-ffile-prefix-map=${CMAKE_SOURCE_DIR}=pyhdiff" "-ffile-prefix-map=${CMAKE_BINARY_DIR}=build"
  "-ffile-prefix-map=${PYHDIFF_RUNTIME}=runtime" "-ffile-prefix-map=${llvm_root}=llvm")
add_compile_definitions(_IS_USED_MULTITHREAD=0)
add_compile_options("$<$<COMPILE_LANGUAGE:CXX>:-nostdinc++>"
  "$<$<COMPILE_LANGUAGE:CXX>:-fvisibility-inlines-hidden>"
  "$<$<COMPILE_LANGUAGE:CXX>:-fexceptions>" "$<$<COMPILE_LANGUAGE:CXX>:-frtti>")
string(TOUPPER ${PYHDIFF_HARDENING} mode)
add_compile_definitions("$<$<COMPILE_LANGUAGE:CXX>:_LIBCPP_HARDENING_MODE=_LIBCPP_HARDENING_MODE_${mode}>")
include_directories(SYSTEM ${PYHDIFF_RUNTIME}/include/c++/v1)
file(GLOB builtins "${PYHDIFF_RUNTIME}/lib/*/libclang_rt.builtins-x86_64.a" "${PYHDIFF_RUNTIME}/lib/libclang_rt.builtins-x86_64.a")
list(LENGTH builtins builtins_count)
if(NOT builtins_count EQUAL 1)
  message(FATAL_ERROR "Expected one private compiler-rt builtins archive")
endif()
set(runtime -nostdlib++ --rtlib=compiler-rt --unwindlib=none
  -Wl,--start-group ${PYHDIFF_RUNTIME}/lib/libc++.a ${PYHDIFF_RUNTIME}/lib/libc++abi.a
  ${PYHDIFF_RUNTIME}/lib/libunwind.a ${builtins} -Wl,--end-group -lm -ldl -lpthread)
add_link_options(-Wl,-z,relro,-z,now,-z,noexecstack -Wl,--exclude-libs,ALL -Wl,--gc-sections
  -Wl,--build-id=sha1)
option(PYHDIFF_SANITIZE "Enable native address and undefined behavior sanitizers" OFF)
if(PYHDIFF_SANITIZE)
  add_compile_options(-fsanitize=address,undefined -fno-sanitize-recover=all -fno-omit-frame-pointer)
  add_link_options(-fsanitize=address,undefined)
endif()

include(FetchContent)
set(ZSTD_BUILD_SHARED OFF CACHE BOOL "" FORCE)
set(ZSTD_BUILD_STATIC ON CACHE BOOL "" FORCE)
set(ZSTD_BUILD_PROGRAMS OFF CACHE BOOL "" FORCE)
set(ZSTD_BUILD_TESTS OFF CACHE BOOL "" FORCE)
set(ZSTD_MULTITHREAD_SUPPORT OFF CACHE BOOL "" FORCE)
# Standard frames only: no decoders for pre-1.0 formats.
set(ZSTD_LEGACY_SUPPORT OFF CACHE BOOL "" FORCE)
FetchContent_Declare(hdiff
  GIT_REPOSITORY https://github.com/komastudios/HDiffPatch.git
  GIT_TAG 091e20cdc659c9547857c0e7d2ef3306701eecd3
  SOURCE_SUBDIR no-cmake-project)
FetchContent_Declare(zstd
  URL https://github.com/facebook/zstd/releases/download/v1.5.7/zstd-1.5.7.tar.gz
  URL_HASH SHA256=eb33e51f49a15e023950cd7825ca74a4a2b43db8354825ac24fc1b7ee09e6fa3
  SOURCE_SUBDIR build/cmake
  EXCLUDE_FROM_ALL)
FetchContent_MakeAvailable(hdiff zstd)
set(hdiff_dir ${hdiff_SOURCE_DIR})
file(GLOB_RECURSE hdiff_cpp CONFIGURE_DEPENDS "${hdiff_dir}/libHDiffPatch/HDiff/*.cpp")
function(phd_hdiff name out_diff_info)
  add_library(${name} STATIC ${hdiff_cpp}
    ${hdiff_dir}/libHDiffPatch/HPatch/patch.c
    ${hdiff_dir}/libHDiffPatch/HPatchLite/hpatch_lite.c
    ${hdiff_dir}/libHDiffPatch/HDiff/private_diff/limit_mem_diff/adler_roll.c)
  target_include_directories(${name} SYSTEM PUBLIC ${hdiff_dir})
  target_compile_definitions(${name} PUBLIC _IS_OUT_DIFF_INFO=${out_diff_info})
endfunction()
# Single-threaded codecs without diagnostic printing: no shared mutable
# globals, so concurrent calls from several threads do not race.
phd_hdiff(phd_hdiff 0)

set(warnings -Wall -Wextra -Werror -Wunsafe-buffer-usage)
add_library(phd_bridge STATIC src/native/bridge.cpp)
target_include_directories(phd_bridge PUBLIC src/native)
target_link_libraries(phd_bridge PUBLIC phd_hdiff libzstd_static)
target_compile_options(phd_bridge PRIVATE ${warnings} -Wlifetime-safety)

if(SKBUILD)
  find_package(Python 3.12 REQUIRED COMPONENTS Interpreter Development.SABIModule)
  python_add_library(_native MODULE WITH_SOABI USE_SABI 3.12 src/pyhdiff/_native.c)
  target_compile_options(_native PRIVATE ${warnings})
  # Codec archives precede the runtime group so every C++ reference resolves inside it.
  target_link_libraries(_native PRIVATE phd_bridge phd_hdiff libzstd_static ${runtime})
  set_target_properties(_native PROPERTIES LINKER_LANGUAGE CXX)
  # Export only the module initializer.
  target_link_options(_native PRIVATE "-Wl,--version-script=${CMAKE_CURRENT_SOURCE_DIR}/src/native/exports.map")
  # Ship a stripped extension; keep its debug information beside it, matched by
  # build ID and debuglink, so a hardening trap in the field can be symbolized.
  add_custom_command(TARGET _native POST_BUILD
    COMMAND ${CMAKE_OBJCOPY} --only-keep-debug $<TARGET_FILE:_native> ${CMAKE_BINARY_DIR}/_native.debug
    COMMAND ${CMAKE_OBJCOPY} --strip-all $<TARGET_FILE:_native>
    COMMAND ${CMAKE_OBJCOPY} --add-gnu-debuglink=_native.debug $<TARGET_FILE:_native>
    WORKING_DIRECTORY ${CMAKE_BINARY_DIR}
    VERBATIM)
  install(TARGETS _native LIBRARY DESTINATION pyhdiff COMPONENT python)
  install(FILES ${hdiff_dir}/LICENSE DESTINATION pyhdiff/licenses RENAME HDiffPatch.txt COMPONENT python)
  install(FILES ${zstd_SOURCE_DIR}/LICENSE DESTINATION pyhdiff/licenses RENAME zstd.txt COMPONENT python)
  install(DIRECTORY ${PYHDIFF_RUNTIME}/licenses/ DESTINATION pyhdiff/licenses COMPONENT python)
endif()

option(PYHDIFF_NATIVE_TESTS "Build the native fault-injection and hardening tests" OFF)
if(PYHDIFF_NATIVE_TESTS)
  enable_testing()
  add_library(phd_bridge_test STATIC src/native/bridge.cpp)
  target_compile_definitions(phd_bridge_test PUBLIC PHD_TEST=1)
  target_include_directories(phd_bridge_test PUBLIC src/native)
  target_link_libraries(phd_bridge_test PUBLIC phd_hdiff libzstd_static)
  target_compile_options(phd_bridge_test PRIVATE ${warnings} -Wlifetime-safety)
  add_executable(native_test tests/native/native.cpp)
  target_link_libraries(native_test PRIVATE phd_bridge_test phd_hdiff libzstd_static ${runtime})
  add_test(NAME native COMMAND native_test)
  # The fork's own regressions toggle diagnostic output, so they get their own copy.
  phd_hdiff(phd_hdiff_regression 1)
  add_executable(hdiff_regression ${hdiff_dir}/test/ubsan_regression.cpp)
  target_compile_options(hdiff_regression PRIVATE -UNDEBUG)
  target_link_libraries(hdiff_regression PRIVATE phd_hdiff_regression ${runtime})
  add_test(NAME hdiff_regression COMMAND hdiff_regression)
  add_executable(hardening_test tests/native/hardening.cpp)
  target_compile_definitions(hardening_test PRIVATE NDEBUG=1)
  target_link_libraries(hardening_test PRIVATE ${runtime})
  # scripts/native_checks.py requires each of the seven probes to die by signal.
endif()
