# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

"""Prepare a staged application tree for archiving."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from typing import Optional, Sequence

import mozpack.path as mozpath

from mozbuild.action import find_dupes, package_libmegazord_artifacts
from mozbuild.action import zip as zip_action
from mozbuild.codecoverage import packager as coverage_packager

_REWRITE_SANITIZER_DYLIB = "build/unix/rewrite_sanitizer_dylib.py"
_DIST_INCLUDE_MANIFEST = "_build_manifests/install/dist_include"
_MOZSEARCH_INDEX_DIR = "mozsearch_index"
_MOZSEARCH_JAVA_INDEX_DIR = "mozsearch_java_index"
_RUST_ANALYZER_CONFIG = "rust-analyzer.json"
_SCIP_INDEX = "index.scip"


def check_symbol_archives(symbol_archives: Sequence[str]) -> int:
    for archive in symbol_archives:
        if not os.path.exists(archive):
            print(
                f"Expected symbol archive {archive} not found. This should have "
                "been created by artifacts.py during artifact installation. "
                "Check that UPLOAD_DIR is set correctly and that symbols were "
                "downloaded.",
                file=sys.stderr,
            )
            return 1
    return 0


def _mach(topsrcdir: str, *args: str, cwd: Optional[str] = None) -> int:
    return subprocess.call(
        [sys.executable, mozpath.join(topsrcdir, "mach"), *args], cwd=cwd
    )


def _scip_index(*, topsrcdir: str, moz_fetches_dir: str, output: str) -> int:
    config = mozpath.join(topsrcdir, _RUST_ANALYZER_CONFIG)
    try:
        rc = _mach(topsrcdir, "rust-analyzer-config", "-o", config)
        if rc:
            return rc

        env = dict(os.environ)
        env["CARGO"] = mozpath.join(moz_fetches_dir, "rustc/bin/cargo")
        env["RUSTC"] = mozpath.join(moz_fetches_dir, "rustc/bin/rustc")
        rc = subprocess.call(
            [
                mozpath.join(moz_fetches_dir, "rust-analyzer/bin/rust-analyzer"),
                "scip",
                ".",
                "--config-path",
                config,
            ],
            cwd=topsrcdir,
            env=env,
        )
        if rc:
            return rc

        zip_action.main(["--error-if-empty", "-C", topsrcdir, output, _SCIP_INDEX])
    finally:
        if os.path.exists(config):
            os.remove(config)

    return 0


def package_prepare(
    *,
    staged_dir: str,
    topsrcdir: Optional[str] = None,
    topobjdir: Optional[str] = None,
    moz_fetches_dir: Optional[str] = None,
    symbol_archives: Sequence[str] = (),
    check_dupes: bool = False,
    defines: Sequence[str] = (),
    warn_dupes: bool = False,
    allowed_dupes: Sequence[str] = (),
    mozharness_zip: Optional[str] = None,
    jsshell: Optional[Sequence[str]] = None,
    chrome_map: bool = False,
    code_coverage_archive: Optional[str] = None,
    mozsearch_index: Optional[str] = None,
    mozsearch_includemap: Optional[str] = None,
    mozsearch_scip_index: Optional[str] = None,
    mozsearch_java_index: Optional[str] = None,
    rewrite_sanitizer_dylib: Optional[str] = None,
    archives: Sequence[Sequence[str]] = (),
    libmegazord_artifacts: Optional[Sequence[str]] = None,
) -> int:
    rc = check_symbol_archives(symbol_archives)
    if rc:
        return rc

    if check_dupes:
        argv = [f"-D{define}" for define in defines]
        if warn_dupes:
            argv.append("-w")
        for allowlist in allowed_dupes:
            argv += ["-f", allowlist]
        rc = find_dupes.main([*argv, staged_dir])
        if rc:
            return rc

    if mozharness_zip:
        from mozbuild.action import test_archive

        test_archive.main(["mozharness", mozharness_zip])

    if jsshell:
        source, output, files_from = jsshell
        if os.path.exists(output):
            os.remove(output)
        zip_action.main([
            "-C",
            source,
            "--strip",
            os.path.abspath(output),
            "--files-from",
            files_from,
        ])

    if chrome_map:
        rc = _mach(topsrcdir, "build-backend", "-b", "ChromeMap")
        if rc:
            return rc

    if code_coverage_archive:
        coverage_packager.cli(["--output-file", code_coverage_archive])

    if mozsearch_index:
        zip_action.main([
            "--error-if-empty",
            "--dotfiles",
            "-C",
            mozpath.join(topobjdir, _MOZSEARCH_INDEX_DIR),
            mozsearch_index,
            "**",
        ])

    if mozsearch_includemap:
        shutil.copyfile(
            mozpath.join(topobjdir, _DIST_INCLUDE_MANIFEST), mozsearch_includemap
        )

    if mozsearch_scip_index:
        rc = _scip_index(
            topsrcdir=topsrcdir,
            moz_fetches_dir=moz_fetches_dir,
            output=mozsearch_scip_index,
        )
        if rc:
            return rc

    if mozsearch_java_index:
        rc = _mach(topsrcdir, "android", "compile-all")
        if rc:
            return rc
        zip_action.main([
            "--error-if-empty",
            "--dotfiles",
            "-C",
            mozpath.join(topobjdir, _MOZSEARCH_JAVA_INDEX_DIR),
            mozsearch_java_index,
            "**",
        ])

    if rewrite_sanitizer_dylib:
        rc = subprocess.call([
            sys.executable,
            mozpath.join(topsrcdir, _REWRITE_SANITIZER_DYLIB),
            rewrite_sanitizer_dylib,
        ])
        if rc:
            return rc

    for source, output, pattern in archives:
        zip_action.main(["--error-if-empty", "-C", source, output, pattern])

    if libmegazord_artifacts:
        dist_bin, output = libmegazord_artifacts
        rc = package_libmegazord_artifacts.main(["--dist-bin", dist_bin, output])
        if rc:
            return rc

    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare a staged application tree for archiving."
    )
    parser.add_argument(
        "--staged-dir", required=True, help="Staged application tree to prepare"
    )
    parser.add_argument("--topsrcdir", required=True, help="Top source directory")
    parser.add_argument("--topobjdir", help="Top object directory")
    parser.add_argument(
        "--moz-fetches-dir", metavar="DIR", help="Directory holding fetched toolchains"
    )
    parser.add_argument(
        "--symbol-archive",
        action="append",
        default=[],
        dest="symbol_archives",
        metavar="PATH",
        help="Archive an artifact build must have downloaded. Repeatable.",
    )
    parser.add_argument(
        "--find-dupes",
        dest="check_dupes",
        action="store_true",
        help="Check the staged tree for duplicated files",
    )
    parser.add_argument(
        "-D",
        dest="defines",
        action="append",
        default=[],
        metavar="VAR[=VAL]",
        help="Define a variable for the duplicate allowlist preprocessor. Repeatable.",
    )
    parser.add_argument(
        "--warn-dupes",
        action="store_true",
        help="Warn about unexpected duplicated files instead of failing",
    )
    parser.add_argument(
        "--allowed-dupes",
        action="append",
        default=[],
        metavar="PATH",
        help="File listing duplicates that are allowed. Repeatable.",
    )
    parser.add_argument(
        "--mozharness-zip", metavar="PATH", help="mozharness archive to write"
    )
    parser.add_argument(
        "--jsshell",
        nargs=3,
        metavar=("SOURCE", "OUTPUT", "FILES_FROM"),
        help="Archive the JavaScript shell files under SOURCE into OUTPUT. "
        "FILES_FROM lists the files, one path per line.",
    )
    parser.add_argument(
        "--chrome-map",
        action="store_true",
        help="Generate the ChromeMap build backend",
    )
    parser.add_argument(
        "--code-coverage-archive", metavar="PATH", help="Coverage archive to write"
    )
    parser.add_argument(
        "--mozsearch-index", metavar="PATH", help="mozsearch index archive to write"
    )
    parser.add_argument(
        "--mozsearch-includemap",
        metavar="PATH",
        help="mozsearch distinclude map to write",
    )
    parser.add_argument(
        "--mozsearch-scip-index", metavar="PATH", help="mozsearch scip index to write"
    )
    parser.add_argument(
        "--mozsearch-java-index",
        metavar="PATH",
        help="mozsearch Java and Kotlin semanticdb archive to write",
    )
    parser.add_argument(
        "--rewrite-sanitizer-dylib",
        metavar="DIR",
        help="Rewrite sanitizer runtime dylib paths for the binaries in DIR",
    )
    parser.add_argument(
        "--archive",
        action="append",
        nargs=3,
        default=[],
        dest="archives",
        metavar=("SOURCE", "OUTPUT", "PATTERN"),
        help="Archive the files matching PATTERN under SOURCE into OUTPUT, "
        "which must not come out empty. Repeatable.",
    )
    parser.add_argument(
        "--libmegazord-artifacts",
        nargs=2,
        metavar=("DIST_BIN", "OUTPUT"),
        help="Archive the libmegazord artifacts in DIST_BIN into OUTPUT",
    )
    return parser


def main(argv: list[str]) -> int:
    args = _parser().parse_args(argv)
    return package_prepare(**vars(args))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
