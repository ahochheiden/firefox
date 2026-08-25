# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

"""Stage the application tree the package archive is built from."""

import os
import plistlib
import sys
from argparse import ArgumentParser
from contextlib import contextmanager
from io import StringIO
from typing import Iterable, Optional, Sequence

import buildconfig
import mozpack.path as mozpath
from createprecomplete import generate_precomplete
from mozpack.copier import FileCopier, Jarrer
from mozpack.errors import errors
from mozpack.files import ExecutableFile, File, FileFinder, GeneratedFile
from mozpack.packager import (
    Component,
    SimpleManifestSink,
    preprocess,
    preprocess_manifest,
)
from mozpack.packager.formats import FlatFormatter, JarFormatter, OmniJarFormatter


class PackagerFileFinder(FileFinder):
    def get(self, path, known_to_exist=False):
        f = super().get(path, known_to_exist)
        # Normalize Info.plist files, and remove the MozillaDeveloper*Path
        # entries which are only needed on unpackaged builds.
        if mozpath.basename(path) == "Info.plist":
            info = plistlib.load(f.open())
            info.pop("MozillaDeveloperObjPath", None)
            info.pop("MozillaDeveloperRepoPath", None)
            return GeneratedFile(plistlib.dumps(info, sort_keys=False))
        return f


class RemovedFiles(GeneratedFile):
    """
    File class for removed-files. Is used as a preprocessor parser.
    """

    def __init__(self, copier):
        self.copier = copier
        GeneratedFile.__init__(self, "")

    def handle_line(self, f):
        f = f.strip()
        if not f:
            return
        if self.copier.contains(f):
            errors.error(f"Removal of packaged file(s): {f}")
        self.content += f.encode("utf-8") + b"\n"


def split_define(define):
    """
    Give a VAR[=VAL] string, returns a (VAR, VAL) tuple, where VAL defaults to
    1. Numeric VALs are returned as ints.
    """
    if "=" in define:
        name, value = define.split("=", 1)
        try:
            value = int(value)
        except ValueError:
            pass
        return (name, value)
    return (define, 1)


class NoPkgFilesRemover:
    """
    Formatter wrapper to handle NO_PKG_FILES.
    """

    def __init__(self, formatter, files, has_manifest):
        self._formatter = formatter
        self._files = files
        if has_manifest:
            self._error = errors.error
            self._msg = "NO_PKG_FILES contains file listed in manifest: "
        else:
            self._error = errors.warn
            self._msg = "Skipping "

    def add_base(self, base, *args):
        self._formatter.add_base(base, *args)

    def add(self, path, content):
        if not any(mozpath.match(path, spec) for spec in self._files):
            self._formatter.add(path, content)
        else:
            self._error(f"{self._msg}{path}")

    def add_manifest(self, entry):
        self._formatter.add_manifest(entry)

    def contains(self, path):
        return self._formatter.contains(path)


@contextmanager
def _error_policy(ignore_errors):
    errors.ignore_errors(ignore_errors)
    try:
        yield
    finally:
        errors.ignore_errors(False)


def _stage(
    *,
    source: str,
    destination: str,
    manifest: Optional[str],
    removals: Optional[str],
    defines: Iterable[str],
    no_pkg_files: Optional[Iterable[str]],
    package_format: str,
    non_resources: Sequence[str],
    jarlog: Optional[str],
    compress: str,
    minify: bool,
    minify_js: bool,
    minify_pdfjs: bool,
    ignore_broken_symlinks: bool,
) -> int:
    all_defines = dict(buildconfig.defines["ALLDEFINES"])

    for name, value in [split_define(d) for d in defines]:
        all_defines[name] = value

    deflate = {
        "none": False,
        "deflate": True,
    }[compress]

    copier = FileCopier()
    if package_format == "flat":
        formatter = FlatFormatter(copier)
    elif package_format == "jar":
        formatter = JarFormatter(copier, compress=deflate)
    elif package_format == "omni":
        formatter = OmniJarFormatter(
            copier,
            buildconfig.substs["OMNIJAR_NAME"],
            compress=deflate,
            non_resources=non_resources,
        )
    else:
        errors.fatal(f"Unknown format: {package_format}")

    # Adjust defines according to the requested format.
    if isinstance(formatter, OmniJarFormatter):
        all_defines["MOZ_OMNIJAR"] = 1
    elif "MOZ_OMNIJAR" in all_defines:
        del all_defines["MOZ_OMNIJAR"]

    respath = ""
    if "RESPATH" in all_defines:
        respath = SimpleManifestSink.normalize_path(all_defines["RESPATH"])
    while respath.startswith("/"):
        respath = respath[1:]

    with errors.accumulate():
        finder_args = dict(
            minify=minify,
            minify_js=minify_js,
            minify_pdfjs=minify_pdfjs,
            ignore_broken_symlinks=ignore_broken_symlinks,
        )
        finder = PackagerFileFinder(source, find_executables=True, **finder_args)
        if no_pkg_files is not None:
            sinkformatter = NoPkgFilesRemover(
                formatter, [*no_pkg_files], manifest is not None
            )
        else:
            sinkformatter = formatter
        sink = SimpleManifestSink(finder, sinkformatter)
        if manifest:
            preprocess_manifest(sink, manifest, all_defines)
        else:
            sink.add(Component(""), "bin/*")
        sink.close(manifest is not None)

        if removals:
            with open(removals) as f:
                removals_in = StringIO(f.read())
            removals_in.name = removals
            removed = RemovedFiles(copier)
            preprocess(removals_in, removed, all_defines)
            copier.add(mozpath.join(respath, "removed-files"), removed)

    # If a pdb file is present and we were instructed to copy it, include it.
    # Run on all OSes to capture MinGW builds
    if buildconfig.substs.get("MOZ_COPY_PDBS"):
        # We want to mutate the copier while we're iterating through it, so copy
        # the items to a list first.
        copier_items = [(p, f) for p, f in copier]
        for p, f in copier_items:
            if isinstance(f, ExecutableFile):
                fpath = f.inputs()[0]
                pdbname = os.path.splitext(fpath)[0] + ".pdb"
                if os.path.exists(pdbname):
                    copier.add(
                        mozpath.join(mozpath.dirname(p), mozpath.basename(pdbname)),
                        File(pdbname),
                    )

    # Setup preloading
    if jarlog:
        if not os.path.exists(jarlog):
            raise Exception(f"Cannot find jar log: {jarlog}")
        omnijars = []
        if isinstance(formatter, OmniJarFormatter):
            omnijars = [
                mozpath.join(base, buildconfig.substs["OMNIJAR_NAME"])
                for base in sink.packager.get_bases(addons=False)
            ]

        from mozpack.mozjar import JarLog

        log = JarLog(jarlog)
        for p, f in copier:
            if not isinstance(f, Jarrer):
                continue
            if respath:
                p = mozpath.relpath(p, respath)
            if p in log:
                f.preload(log[p])
            elif p in omnijars:
                raise Exception(f"No jar log data for {p}")

    copier.copy(destination)
    generate_precomplete(os.path.normpath(os.path.join(destination, respath)))
    return 0


def package_stage(
    *,
    source: str,
    destination: str,
    manifest: Optional[str] = None,
    removals: Optional[str] = None,
    defines: Iterable[str] = (),
    no_pkg_files: Optional[Iterable[str]] = None,
    package_format: str = "omni",
    non_resources: Sequence[str] = (),
    jarlog: Optional[str] = None,
    compress: str = "deflate",
    minify: bool = False,
    minify_js: bool = False,
    minify_pdfjs: bool = False,
    ignore_errors: bool = False,
    ignore_broken_symlinks: bool = False,
) -> int:
    with _error_policy(ignore_errors):
        return _stage(
            source=source,
            destination=destination,
            manifest=manifest,
            removals=removals,
            defines=defines,
            no_pkg_files=no_pkg_files,
            package_format=package_format,
            non_resources=non_resources,
            jarlog=jarlog,
            compress=compress,
            minify=minify,
            minify_js=minify_js,
            minify_pdfjs=minify_pdfjs,
            ignore_broken_symlinks=ignore_broken_symlinks,
        )


def _parser():
    parser = ArgumentParser(
        description="Stage the application tree the package archive is built from."
    )
    parser.add_argument(
        "-D",
        dest="defines",
        action="append",
        default=[],
        metavar="VAR[=VAL]",
        help="Define a variable",
    )
    parser.add_argument(
        "--format",
        default="omni",
        help="Choose the chrome format for packaging "
        + "(omni, jar or flat ; default: %(default)s)",
    )
    parser.add_argument("--removals", default=None, help="removed-files source file")
    parser.add_argument(
        "--ignore-errors",
        action="store_true",
        default=False,
        help="Transform errors into warnings.",
    )
    parser.add_argument(
        "--ignore-broken-symlinks",
        action="store_true",
        default=False,
        help="Do not fail when processing broken symlinks.",
    )
    parser.add_argument(
        "--minify",
        action="store_true",
        default=False,
        help="Make some files more compact while packaging",
    )
    parser.add_argument(
        "--minify-js",
        action="store_true",
        help="Minify JavaScript files while packaging.",
    )
    parser.add_argument(
        "--minify-pdfjs",
        action="store_true",
        help="Minify PDF.js JavaScript files while packaging.",
    )
    parser.add_argument(
        "--jarlog", default="", help="File containing jar " + "access logs"
    )
    parser.add_argument(
        "--compress",
        choices=("none", "deflate"),
        default="deflate",
        help="Use given jar compression (default: deflate)",
    )
    parser.add_argument("manifest", default=None, nargs="?", help="Manifest file name")
    parser.add_argument("source", help="Source directory")
    parser.add_argument("destination", help="Destination directory")
    parser.add_argument(
        "--non-resource",
        nargs="+",
        metavar="PATTERN",
        default=[],
        help="Extra files not to be considered as resources",
    )
    parser.add_argument(
        "--no-pkg-files",
        action="append",
        default=None,
        metavar="PATTERN",
        help="Pattern of files never to package. Repeatable. Passing this at "
        "all enables the check, and a match is an error when a manifest is "
        "given and a warning otherwise.",
    )
    return parser


def main(argv):
    args = _parser().parse_args(argv)
    return package_stage(
        source=args.source,
        destination=args.destination,
        manifest=args.manifest,
        removals=args.removals,
        defines=args.defines,
        no_pkg_files=args.no_pkg_files,
        package_format=args.format,
        non_resources=args.non_resource,
        jarlog=args.jarlog or None,
        compress=args.compress,
        minify=args.minify,
        minify_js=args.minify_js,
        minify_pdfjs=args.minify_pdfjs,
        ignore_errors=args.ignore_errors,
        ignore_broken_symlinks=args.ignore_broken_symlinks,
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
