#!/bin/bash
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
set -e -v -x

# Build the mach-patched ninja from source. The source is a fetch
# task (`fetches.fetch: - ninja-source`) pointing at a ninja fork
# pinned to a specific commit. See
# `taskcluster/kinds/fetch/toolchains.yml` for the source pin.
#
# Output: public/build/ninja.tar.zst, structured as `ninja/bin/ninja[.exe]`
# so consumers (clang/gn/onnx toolchain tasks) keep working with the
# same path layout the previous static-url fetch produced.

SRC_DIR=$MOZ_FETCHES_DIR/ninja-source
STAGE=$PWD/ninja

cd $GECKO_PATH

case "$1" in
    --win64)
        # `vs-setup.sh` configures INCLUDE/LIB/PATH to point at the
        # MSVC toolchain artifact (vs-toolchain) so configure.py's
        # bootstrap can find cl.exe.
        . taskcluster/scripts/misc/vs-setup.sh
        # Embed only the PDB basename in the debug directory so the
        # binary is reproducible across build hosts (configure.py
        # passes $LDFLAGS straight through to link.exe).
        export LDFLAGS='/PDBALTPATH:%_PDB%'
        cd $SRC_DIR
        python3 configure.py --bootstrap
        BIN=ninja.exe
        ;;
    --win64-aarch64)
        # Cross-compile to arm64 windows from the x64 windows worker.
        # `vs-setup.sh` switches to the arm64 cross when TARGET is set.
        # `--bootstrap` would produce an arm64 stage-1 binary that the
        # x64 host can't run, so we drive the build with the x64
        # `win64-ninja` artifact instead.
        export TARGET=aarch64-pc-windows-msvc
        . taskcluster/scripts/misc/vs-setup.sh
        export PATH="$(cd $MOZ_FETCHES_DIR/ninja && pwd)/bin:$PATH"
        export LDFLAGS='/PDBALTPATH:%_PDB%'
        cd $SRC_DIR
        python3 configure.py --platform=msvc
        ninja
        BIN=ninja.exe
        ;;
    --macos)
        # Native build on the macOS arm64 worker. clang's `-arch arm64
        # -arch x86_64` produces a universal Mach-O directly, so a
        # single `--bootstrap` pass yields a binary that runs on both
        # Apple Silicon and Intel macs.
        cd $SRC_DIR
        export CXX="clang++ -arch arm64 -arch x86_64"
        export CC="clang -arch arm64 -arch x86_64"
        python3 configure.py --bootstrap
        BIN=ninja
        ;;
    --linux-aarch64)
        # Cross-compile to arm64 linux from the x86_64 linux worker.
        # Use `linux64-clang-toolchain`'s clang with `--target` and the
        # `sysroot-aarch64-linux-gnu` sysroot. The cross-compiled
        # stage-1 binary won't run on the x86_64 host, so we drive the
        # build with the host `linux64-ninja` artifact.
        export PATH="$MOZ_FETCHES_DIR/clang/bin:$PATH"
        SYSROOT="$MOZ_FETCHES_DIR/sysroot-aarch64-linux-gnu"
        export CC="clang --target=aarch64-linux-gnu --sysroot=$SYSROOT -fuse-ld=lld"
        export CXX="clang++ --target=aarch64-linux-gnu --sysroot=$SYSROOT -fuse-ld=lld"
        export PATH="$(cd $MOZ_FETCHES_DIR/ninja && pwd)/bin:$PATH"
        cd $SRC_DIR
        python3 configure.py --platform=linux
        ninja
        BIN=ninja
        ;;
    *)
        # Native linux build using the system gcc/g++ from the worker
        # docker image (matches the linux64-gn pattern). Avoids a
        # dependency on linux64-clang-toolchain, which would create a
        # cycle since the clang stage-1 tasks depend on linux64-ninja.
        export CC=gcc
        export CXX=g++
        cd $SRC_DIR
        python3 configure.py --bootstrap
        BIN=ninja
        ;;
esac

# Stage the binary at `ninja/bin/ninja[.exe]` to match the layout the
# previous prebuilt-fetch produced (`add-prefix: ninja/bin/`). Consumers
# resolve the binary at `$MOZ_FETCHES_DIR/ninja/bin/ninja[.exe]`.
mkdir -p $STAGE/bin
cp $SRC_DIR/$BIN $STAGE/bin/

cd $(dirname $STAGE)
tar -c ninja | python3 $GECKO_PATH/taskcluster/scripts/misc/zstdpy > ninja.tar.zst
mkdir -p $UPLOAD_DIR
cp ninja.tar.zst $UPLOAD_DIR

if [ "$1" = "--win64" ] || [ "$1" = "--win64-aarch64" ]; then
    . $GECKO_PATH/taskcluster/scripts/misc/vs-cleanup.sh
fi
