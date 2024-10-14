# Copyright 2023 gRPC authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import os.path
import platform
import re
import shlex
import subprocess
from subprocess import PIPE
import sys
import sysconfig

import setuptools
from setuptools import Extension
from setuptools.command import build_ext

PYTHON_STEM = os.path.realpath(os.path.dirname(__file__))
README_PATH = os.path.join(PYTHON_STEM, "README.rst")

os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.abspath("."))

import _parallel_compile_patch
import observability_lib_deps

import grpc_version

_parallel_compile_patch.monkeypatch_compile_maybe()

CLASSIFIERS = [
    "Development Status :: 4 - Beta",
    "Programming Language :: Python",
    "Programming Language :: Python :: 3",
    "License :: OSI Approved :: Apache Software License",
]

O11Y_CC_SRCS = [
    "client_call_tracer.cc",
    "metadata_exchange.cc",
    "observability_util.cc",
    "python_observability_context.cc",
    "rpc_encoding.cc",
    "sampler.cc",
    "server_call_tracer.cc",
]


def _env_bool_value(env_name, default):
    """Parses a bool option from an environment variable"""
    return os.environ.get(env_name, default).upper() not in ["FALSE", "0", ""]


def _is_alpine():
    """Checks if it's building Alpine"""
    os_release_content = ""
    try:
        with open("/etc/os-release", "r") as f:
            os_release_content = f.read()
        if "alpine" in os_release_content:
            return True
    except Exception:
        return False


# Environment variable to determine whether or not the Cython extension should
# *use* Cython or use the generated C files. Note that this requires the C files
# to have been generated by building first *with* Cython support.
BUILD_WITH_CYTHON = _env_bool_value("GRPC_PYTHON_BUILD_WITH_CYTHON", "False")

# Export this variable to force building the python extension with a statically linked libstdc++.
# At least on linux, this is normally not needed as we can build manylinux-compatible wheels on linux just fine
# without statically linking libstdc++ (which leads to a slight increase in the wheel size).
# This option is useful when crosscompiling wheels for aarch64 where
# it's difficult to ensure that the crosscompilation toolchain has a high-enough version
# of GCC (we require >=5.1) but still uses old-enough libstdc++ symbols.
# TODO(jtattermusch): remove this workaround once issues with crosscompiler version are resolved.
BUILD_WITH_STATIC_LIBSTDCXX = _env_bool_value(
    "GRPC_PYTHON_BUILD_WITH_STATIC_LIBSTDCXX", "False"
)


def check_linker_need_libatomic():
    """Test if linker on system needs libatomic."""
    code_test = (
        b"#include <atomic>\n"
        + b"int main() { return std::atomic<int64_t>{}; }"
    )
    cxx = shlex.split(os.environ.get("CXX", "c++"))
    cpp_test = subprocess.Popen(
        cxx + ["-x", "c++", "-std=c++14", "-"],
        stdin=PIPE,
        stdout=PIPE,
        stderr=PIPE,
    )
    cpp_test.communicate(input=code_test)
    if cpp_test.returncode == 0:
        return False
    # Double-check to see if -latomic actually can solve the problem.
    # https://github.com/grpc/grpc/issues/22491
    cpp_test = subprocess.Popen(
        cxx + ["-x", "c++", "-std=c++14", "-", "-latomic"],
        stdin=PIPE,
        stdout=PIPE,
        stderr=PIPE,
    )
    cpp_test.communicate(input=code_test)
    return cpp_test.returncode == 0


class BuildExt(build_ext.build_ext):
    """Custom build_ext command."""

    def get_ext_filename(self, ext_name):
        # since python3.5, python extensions' shared libraries use a suffix that corresponds to the value
        # of sysconfig.get_config_var('EXT_SUFFIX') and contains info about the architecture the library targets.
        # E.g. on x64 linux the suffix is ".cpython-XYZ-x86_64-linux-gnu.so"
        # When crosscompiling python wheels, we need to be able to override this suffix
        # so that the resulting file name matches the target architecture and we end up with a well-formed
        # wheel.
        filename = build_ext.build_ext.get_ext_filename(self, ext_name)
        orig_ext_suffix = sysconfig.get_config_var("EXT_SUFFIX")
        new_ext_suffix = os.getenv("GRPC_PYTHON_OVERRIDE_EXT_SUFFIX")
        if new_ext_suffix and filename.endswith(orig_ext_suffix):
            filename = filename[: -len(orig_ext_suffix)] + new_ext_suffix
        return filename


# There are some situations (like on Windows) where CC, CFLAGS, and LDFLAGS are
# entirely ignored/dropped/forgotten by distutils and its Cygwin/MinGW support.
# We use these environment variables to thus get around that without locking
# ourselves in w.r.t. the multitude of operating systems this ought to build on.
# We can also use these variables as a way to inject environment-specific
# compiler/linker flags. We assume GCC-like compilers and/or MinGW as a
# reasonable default.
EXTRA_ENV_COMPILE_ARGS = os.environ.get("GRPC_PYTHON_CFLAGS", None)
EXTRA_ENV_LINK_ARGS = os.environ.get("GRPC_PYTHON_LDFLAGS", None)
if EXTRA_ENV_COMPILE_ARGS is None:
    EXTRA_ENV_COMPILE_ARGS = "-std=c++14"
    if "win32" in sys.platform:
        # We need to statically link the C++ Runtime, only the C runtime is
        # available dynamically
        EXTRA_ENV_COMPILE_ARGS += " /MT"
    elif "linux" in sys.platform or "darwin" in sys.platform:
        EXTRA_ENV_COMPILE_ARGS += " -fno-wrapv -frtti -fvisibility=hidden"

if EXTRA_ENV_LINK_ARGS is None:
    EXTRA_ENV_LINK_ARGS = ""
    if "linux" in sys.platform or "darwin" in sys.platform:
        EXTRA_ENV_LINK_ARGS += " -lpthread"
        if check_linker_need_libatomic():
            EXTRA_ENV_LINK_ARGS += " -latomic"

# This enables the standard link-time optimizer, which help us prevent some undefined symbol errors by
# remove some unused symbols from .so file.
# Note that it does not work for MSCV on windows.
if "win32" not in sys.platform:
    EXTRA_ENV_COMPILE_ARGS += " -flto"
    # Compile with fail with error: `lto-wrapper failed` when lto flag was enabled in Alpine using musl libc.
    # As a work around we need to disable ipa-cp.
    if _is_alpine():
        EXTRA_ENV_COMPILE_ARGS += " -fno-ipa-cp"

EXTRA_COMPILE_ARGS = shlex.split(EXTRA_ENV_COMPILE_ARGS)
EXTRA_LINK_ARGS = shlex.split(EXTRA_ENV_LINK_ARGS)

if BUILD_WITH_STATIC_LIBSTDCXX:
    EXTRA_LINK_ARGS.append("-static-libstdc++")

CC_FILES = [
    os.path.normpath(cc_file) for cc_file in observability_lib_deps.CC_FILES
]
CC_INCLUDES = [
    os.path.normpath(include_dir)
    for include_dir in observability_lib_deps.CC_INCLUDES
]

DEFINE_MACROS = (("_WIN32_WINNT", 0x600),)

if "win32" in sys.platform:
    DEFINE_MACROS += (
        ("WIN32_LEAN_AND_MEAN", 1),
        ("CARES_STATICLIB", 1),
        ("GRPC_ARES", 0),
        ("NTDDI_VERSION", 0x06000000),
        # avoid https://github.com/abseil/abseil-cpp/issues/1425
        ("NOMINMAX", 1),
    )
    if "64bit" in platform.architecture()[0]:
        DEFINE_MACROS += (("MS_WIN64", 1),)
    else:
        # For some reason, this is needed to get access to inet_pton/inet_ntop
        # on msvc, but only for 32 bits
        DEFINE_MACROS += (("NTDDI_VERSION", 0x06000000),)
elif "linux" in sys.platform or "darwin" in sys.platform:
    DEFINE_MACROS += (("HAVE_PTHREAD", 1),)

# Fix for Cython build issue in aarch64.
# It's required to define this macro before include <inttypes.h>.
# <inttypes.h> was included in core/telemetry/call_tracer.h.
# This macro should already be defined in grpc/grpc.h through port_platform.h,
# but we're still having issue in aarch64, so we manually define the macro here.
# TODO(xuanwn): Figure out what's going on in the aarch64 build so we can support
# gcc + Bazel.
DEFINE_MACROS += (("__STDC_FORMAT_MACROS", None),)


# Use `-fvisibility=hidden` will hide cython init symbol, we need that symbol exported
# in order to import cython module.
if "linux" in sys.platform or "darwin" in sys.platform:
    pymodinit = 'extern "C" __attribute__((visibility ("default"))) PyObject*'
    DEFINE_MACROS += (("PyMODINIT_FUNC", pymodinit),)


def extension_modules():
    if BUILD_WITH_CYTHON:
        cython_module_files = [
            os.path.join("grpc_observability", "_cyobservability.pyx")
        ]
    else:
        cython_module_files = [
            os.path.join("grpc_observability", "_cyobservability.cpp")
        ]

    plugin_include = [
        ".",
        "grpc_root",
        os.path.join("grpc_root", "include"),
    ] + CC_INCLUDES

    plugin_sources = CC_FILES

    O11Y_CC_PATHS = (
        os.path.join("grpc_observability", f) for f in O11Y_CC_SRCS
    )
    plugin_sources += O11Y_CC_PATHS

    plugin_sources += cython_module_files

    plugin_ext = Extension(
        name="grpc_observability._cyobservability",
        sources=plugin_sources,
        include_dirs=plugin_include,
        language="c++",
        define_macros=list(DEFINE_MACROS),
        extra_compile_args=list(EXTRA_COMPILE_ARGS),
        extra_link_args=list(EXTRA_LINK_ARGS),
    )
    extensions = [plugin_ext]
    if BUILD_WITH_CYTHON:
        from Cython import Build

        return Build.cythonize(
            extensions, compiler_directives={"language_level": "3"}
        )
    else:
        return extensions


PACKAGES = setuptools.find_packages(PYTHON_STEM)

setuptools.setup(
    name="grpcio-observability",
    version=grpc_version.VERSION,
    description="gRPC Python observability package",
    long_description_content_type="text/x-rst",
    long_description=open(README_PATH, "r").read(),
    author="The gRPC Authors",
    author_email="grpc-io@googlegroups.com",
    url="https://grpc.io",
    project_urls={
        "Source Code": "https://github.com/grpc/grpc/tree/master/src/python/grpcio_observability",
        "Bug Tracker": "https://github.com/grpc/grpc/issues",
    },
    license="Apache License 2.0",
    classifiers=CLASSIFIERS,
    ext_modules=extension_modules(),
    packages=list(PACKAGES),
    python_requires=">=3.8",
    install_requires=[
        "grpcio=={version}".format(version=grpc_version.VERSION),
        "setuptools>=59.6.0",
        "opentelemetry-api>=1.21.0",
    ],
    cmdclass={
        "build_ext": BuildExt,
    },
)
