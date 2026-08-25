# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

"""Stage, prepare and archive the application package and write its metadata.

Runs the steps in order: locale inputs, stage, prepare, sourcestamp, archive,
package_name.txt, Windows installer, mozinfo copy, build information.
"""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable, Optional, Sequence

import buildconfig
import mozpack.path as mozpath

from mozbuild.action import (
    informulate,
    locale_manifest,
    make_sourcestamp_file,
    nsis_build,
    package_prepare,
)
from mozbuild.action import package as package_action
from mozbuild.action.package_stage import package_stage
from mozbuild.packaging import (
    PackagePaths,
    all_locales,
    multilocale_content,
)
from mozbuild.util import FileAvoidWrite


def _stage(
    *,
    multilocale_txt: str,
    locale_manifest_path: str,
    base_path: str,
    locales: Iterable[str],
    locale_entries: Iterable[str] = (),
    defines: Iterable[str] = (),
    **package_stage_args,
) -> int:
    locales = all_locales(locales)

    with FileAvoidWrite(multilocale_txt) as out:
        out.write(multilocale_content(locales))

    argv = [
        "--output",
        locale_manifest_path,
        "--base-path",
        base_path,
        "--locales",
        *locales,
    ]
    locale_entries = [*locale_entries]
    if locale_entries:
        argv += ["--locale-entries", *locale_entries]

    rc = locale_manifest.main(argv)
    if rc:
        return rc

    return package_stage(
        defines=[f"PKG_LOCALE_MANIFEST={locale_manifest_path}", *defines],
        **package_stage_args,
    )


def _repackage_installer(
    *,
    mach: str,
    output: str,
    tag: str,
    setupexe: str,
    sfx_stub: str,
    use_upx: bool,
    package: Optional[str] = None,
    package_name: Optional[str] = None,
) -> int:
    argv = [sys.executable, mach, "repackage", "installer", "-o", output]
    if package_name:
        argv += ["--package-name", package_name]
    if package:
        argv += ["--package", package]
    argv += ["--tag", tag, "--setupexe", setupexe, "--sfx-stub", sfx_stub]
    if use_upx:
        argv.append("--use-upx")

    return subprocess.call(argv)


def _windows_installer(
    *,
    paths: PackagePaths,
    package: str,
    stub_installer: bool,
    automation: bool,
) -> int:
    substs = buildconfig.substs
    topsrcdir = buildconfig.topsrcdir
    app = substs["MOZ_BUILD_APP"]
    installer_srcdir = mozpath.join(topsrcdir, app, "installer", "windows")
    config_dir = Path(buildconfig.topobjdir) / app / "installer" / "windows" / "instgen"

    makensis = substs["MAKENSISU"]
    makensis_flags = shlex.split(substs.get("MAKENSISU_FLAGS") or "")
    buildid_header = Path(buildconfig.topobjdir) / "buildid.h"
    rc = nsis_build.nsis_build(
        config_dir=config_dir,
        nsi="installer.nsi",
        makensis=makensis,
        makensis_flags=makensis_flags,
        buildid_header=buildid_header,
        output=str(config_dir / "setup.exe"),
    )
    if rc:
        return rc
    if stub_installer:
        rc = nsis_build.nsis_build(
            config_dir=config_dir,
            nsi="stub.nsi",
            makensis=makensis,
            makensis_flags=makensis_flags,
            buildid_header=buildid_header,
            output=str(config_dir / "setup-stub.exe"),
        )
        if rc:
            return rc
    if automation:
        return 0

    os.makedirs(paths.pkg_output_dir, exist_ok=True)
    mach = mozpath.join(topsrcdir, "mach")
    sfx_stub = mozpath.join(topsrcdir, substs["NSIS_SFX_MODULE"])
    use_upx = bool(substs.get("NSIS_USE_UPX"))

    rc = _repackage_installer(
        mach=mach,
        output=paths.installer_package,
        tag=mozpath.join(installer_srcdir, "app.tag"),
        setupexe=str(config_dir / "setup.exe"),
        sfx_stub=sfx_stub,
        use_upx=use_upx,
        package=package,
        package_name=paths.pkg_dir,
    )
    if rc:
        return rc

    if stub_installer:
        return _repackage_installer(
            mach=mach,
            output=mozpath.join(
                paths.pkg_output_dir, f"{paths.stub_installer_basename}.exe"
            ),
            tag=mozpath.join(installer_srcdir, "stub.tag"),
            setupexe=str(config_dir / "setup-stub.exe"),
            sfx_stub=sfx_stub,
            use_upx=use_upx,
        )
    return 0


def package_application(
    *,
    ab_cd: str = "en-US",
    pkg_dir: Optional[str] = None,
    pkg_path: str = "",
    locales: Sequence[str] = (),
    locale_entries: Sequence[str] = (),
    defines: Sequence[str] = (),
    manifest: Optional[str] = None,
    removals: Optional[str] = None,
    non_resources: Sequence[str] = (),
    no_pkg_files: Sequence[str] = (),
    minify: bool = False,
    minify_js: bool = False,
    minify_pdfjs: bool = False,
    jarlog: Optional[str] = None,
    compress: str = "deflate",
    ignore_errors: bool = False,
    ignore_broken_symlinks: bool = False,
    find_dupes: bool = False,
    warn_dupes: bool = False,
    allowed_dupes: Sequence[str] = (),
    mozharness_zip: bool = False,
    jsshell: bool = False,
    automation: bool = False,
    artifact_build_symbols: Optional[str] = None,
    upload_dir: Optional[str] = None,
    code_coverage: bool = False,
    mozsearch: bool = False,
    moz_fetches_dir: Optional[str] = None,
    rewrite_sanitizer_dylib: bool = False,
    installer: bool = False,
    stub_installer: bool = False,
    source_repo_header: Optional[str] = None,
    package_extra_args: Sequence[str] = (),
) -> int:
    substs = buildconfig.substs
    topsrcdir = buildconfig.topsrcdir
    topobjdir = buildconfig.topobjdir
    dist = mozpath.join(topobjdir, "dist")
    app = substs["MOZ_BUILD_APP"]
    paths = PackagePaths(
        substs=substs,
        dist_dir=dist,
        ab_cd=ab_cd,
        pkg_dir=pkg_dir,
        pkg_path=pkg_path,
    )
    staged = mozpath.join(dist, paths.pkg_dir)
    binpath = substs["MOZ_PKG_BINPATH"]
    respath = substs["MOZ_PKG_RESPATH"]
    android = app == "mobile/android"
    installer_objdir = mozpath.join(topobjdir, app, "installer")

    destination = staged
    if not manifest and substs.get("MOZ_MACBUNDLE_NAME") and binpath:
        destination = mozpath.join(staged, binpath)

    symbol_archives = []
    if automation and artifact_build_symbols:
        if not upload_dir:
            print(
                "UPLOAD_DIR is not set, so the symbol archives the artifact "
                "build downloaded cannot be found.",
                file=sys.stderr,
            )
            return 1
        symbol_archives.append(
            mozpath.join(upload_dir, f"{paths.symbol_archive_basename}.zip")
        )
        if artifact_build_symbols == "full":
            symbol_archives.append(
                mozpath.join(
                    upload_dir, f"{paths.symbol_full_archive_basename}.tar.zst"
                )
            )
    rc = package_prepare.check_symbol_archives(symbol_archives)
    if rc:
        return rc

    rc = _stage(
        multilocale_txt=mozpath.join(
            dist, binpath if android else respath, "res", "multilocale.txt"
        ),
        locale_manifest_path=mozpath.join(installer_objdir, "locale-manifest.in"),
        base_path="@BINPATH@" if android else "@RESPATH@",
        locales=locales,
        locale_entries=locale_entries,
        source=dist,
        destination=destination,
        manifest=manifest,
        removals=removals,
        defines=defines,
        no_pkg_files=no_pkg_files,
        package_format=substs["MOZ_PACKAGER_FORMAT"],
        non_resources=non_resources,
        jarlog=jarlog,
        compress=compress,
        minify=minify,
        minify_js=minify_js,
        minify_pdfjs=minify_pdfjs,
        ignore_errors=ignore_errors,
        ignore_broken_symlinks=ignore_broken_symlinks,
    )
    if rc:
        return rc

    archives = []
    if substs.get("MOZ_ARTIFACT_BUILDS"):
        archives.append([
            mozpath.join(dist, "xpt_artifacts"),
            mozpath.join(
                paths.pkg_output_dir, f"{paths.xpt_artifacts_archive_basename}.zip"
            ),
            "*.xpt",
        ])
    elif substs.get("COMPILE_ENVIRONMENT"):
        archives.append([
            mozpath.join(topobjdir, "config", "makefiles", "xpidl"),
            mozpath.join(
                paths.pkg_output_dir, f"{paths.xpt_artifacts_archive_basename}.zip"
            ),
            "*.xpt",
        ])
    cocoa = substs.get("MOZ_WIDGET_TOOLKIT") == "cocoa"
    if (
        paths.update_framework_artifacts_archive_basename
        and cocoa
        and (substs.get("MOZ_ARTIFACT_BUILDS") or substs.get("COMPILE_ENVIRONMENT"))
    ):
        archives.append([
            mozpath.join(dist, "update_framework_artifacts"),
            mozpath.join(
                paths.pkg_output_dir,
                f"{paths.update_framework_artifacts_archive_basename}.zip",
            ),
            "*.framework",
        ])

    libmegazord = None
    if substs.get("MOZ_APPSERVICES_IN_TREE") and substs.get(
        "MOZ_LIBMEGAZORD_ARTIFACTS"
    ):
        libmegazord = [
            mozpath.join(dist, "bin"),
            mozpath.join(
                paths.pkg_output_dir,
                f"{paths.libmegazord_artifacts_archive_basename}.zip",
            ),
        ]

    rc = package_prepare.package_prepare(
        staged_dir=staged,
        topsrcdir=topsrcdir,
        topobjdir=topobjdir,
        moz_fetches_dir=moz_fetches_dir,
        symbol_archives=symbol_archives,
        check_dupes=find_dupes,
        defines=defines,
        warn_dupes=warn_dupes,
        allowed_dupes=allowed_dupes,
        mozharness_zip=(
            mozpath.join(paths.pkg_output_dir, paths.mozharness_package)
            if mozharness_zip
            else None
        ),
        jsshell=(
            [
                mozpath.join(dist, "bin"),
                paths.jsshell,
                mozpath.join(topobjdir, "jsshell-archive.list"),
            ]
            if jsshell
            else None
        ),
        chrome_map=code_coverage or mozsearch,
        code_coverage_archive=(
            mozpath.join(
                paths.pkg_output_dir, f"{paths.code_coverage_archive_basename}.zip"
            )
            if code_coverage
            else None
        ),
        mozsearch_index=(
            mozpath.join(
                paths.pkg_output_dir, f"{paths.mozsearch_archive_basename}.zip"
            )
            if mozsearch
            else None
        ),
        mozsearch_includemap=(
            mozpath.join(
                paths.pkg_output_dir, f"{paths.mozsearch_includemap_basename}.map"
            )
            if mozsearch
            else None
        ),
        mozsearch_scip_index=(
            mozpath.join(
                paths.pkg_output_dir, f"{paths.mozsearch_scip_index_basename}.zip"
            )
            if mozsearch
            else None
        ),
        mozsearch_java_index=(
            mozpath.join(
                paths.pkg_output_dir, f"{paths.mozsearch_java_index_basename}.zip"
            )
            if mozsearch and android
            else None
        ),
        rewrite_sanitizer_dylib=(
            mozpath.join(staged, binpath) if rewrite_sanitizer_dylib else None
        ),
        archives=archives,
        libmegazord_artifacts=libmegazord,
    )
    if rc:
        return rc

    sourcestamp_argv = [
        "--output",
        paths.sourcestamp_file,
        "--buildid-header",
        mozpath.join(topobjdir, "buildid.h"),
    ]
    if source_repo_header:
        sourcestamp_argv += ["--source-repo-header", source_repo_header]
    rc = make_sourcestamp_file.main(sourcestamp_argv)
    if rc:
        return rc

    pkg_format = substs["MOZ_PKG_FORMAT"]
    if pkg_format != "APK":
        rc = package_action.main([
            "--format",
            pkg_format,
            "--cwd",
            dist,
            "--pkg-dir",
            paths.pkg_dir,
            "--output-dir",
            pkg_path,
            "--basename",
            paths.basename,
            "--tar",
            substs["TAR"],
            *package_extra_args,
        ])
        if rc:
            return rc

    with open(
        mozpath.join(dist, "package_name.txt"), "w", encoding="utf-8", newline=""
    ) as out:
        out.write(f"{paths.package}\n")

    if installer and substs.get("OS_ARCH") == "WINNT" and pkg_format == "ZIP":
        rc = _windows_installer(
            paths=paths,
            package=mozpath.join(dist, paths.package),
            stub_installer=stub_installer,
            automation=automation,
        )
        if rc:
            return rc

    if automation:
        shutil.copyfile(mozpath.join(topobjdir, "mozinfo.json"), paths.mozinfo_file)

        informulate_argv = [
            paths.buildinfo_file,
            paths.buildhub_json,
            paths.buildid_info_txt_file,
            f"--ab-cd={ab_cd}",
        ]
        if android and not substs.get("MOZ_ANDROID_WITH_FENNEC"):
            informulate_argv.append("--no-download")
        else:
            informulate_argv += [
                f"--package={mozpath.join(dist, paths.package)}",
                f"--installer={paths.installer_package or ''}",
            ]
        rc = informulate.main(informulate_argv)
        if rc:
            return rc

    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Stage, prepare and archive the application package and "
        "write its metadata."
    )
    parser.add_argument("--ab-cd", default="en-US", help="The locale being packaged")
    parser.add_argument("--pkg-dir", help="Staged package directory name")
    parser.add_argument("--pkg-path", default="", help="Prefix inside the dist folder")
    parser.add_argument(
        "--locale",
        action="append",
        default=[],
        dest="locales",
        metavar="AB_CD",
        help="Locale to list in the chrome multilocale inputs. Repeatable.",
    )
    parser.add_argument(
        "--locale-entry",
        action="append",
        default=[],
        dest="locale_entries",
        metavar="PREFIX",
        help="Chrome locale entry prefix. Repeatable.",
    )
    parser.add_argument(
        "-D",
        dest="defines",
        action="append",
        default=[],
        metavar="VAR[=VAL]",
        help="Define a variable. Repeatable, and later definitions win.",
    )
    parser.add_argument("--manifest", help="Package manifest file")
    parser.add_argument("--removals", help="removed-files source file")
    parser.add_argument(
        "--non-resource",
        action="append",
        default=[],
        dest="non_resources",
        metavar="PATTERN",
        help="Extra files not to be considered as resources. Repeatable.",
    )
    parser.add_argument(
        "--no-pkg-files",
        action="append",
        default=[],
        metavar="PATTERN",
        help="Pattern of files never to package. Repeatable.",
    )
    parser.add_argument("--minify", action="store_true", help="Compact staged files")
    parser.add_argument(
        "--minify-js", action="store_true", help="Minify JavaScript files"
    )
    parser.add_argument(
        "--minify-pdfjs", action="store_true", help="Minify PDF.js JavaScript files"
    )
    parser.add_argument("--jarlog", help="File containing jar access logs")
    parser.add_argument(
        "--compress",
        choices=("none", "deflate"),
        default="deflate",
        help="Use given jar compression (default: %(default)s)",
    )
    parser.add_argument(
        "--ignore-errors", action="store_true", help="Transform errors into warnings"
    )
    parser.add_argument(
        "--ignore-broken-symlinks",
        action="store_true",
        help="Do not fail when processing broken symlinks",
    )
    parser.add_argument(
        "--find-dupes",
        action="store_true",
        help="Check the staged tree for duplicated files",
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
        "--mozharness-zip", action="store_true", help="Archive mozharness"
    )
    parser.add_argument(
        "--jsshell", action="store_true", help="Archive the JavaScript shell"
    )
    parser.add_argument(
        "--automation",
        action="store_true",
        help="Also write the artifacts automation uploads",
    )
    parser.add_argument(
        "--artifact-build-symbols",
        metavar="KIND",
        help="Require the symbol archives an artifact build downloaded",
    )
    parser.add_argument(
        "--upload-dir", metavar="DIR", help="Directory holding downloaded artifacts"
    )
    parser.add_argument(
        "--code-coverage", action="store_true", help="Archive code coverage data"
    )
    parser.add_argument(
        "--mozsearch", action="store_true", help="Archive the mozsearch indexes"
    )
    parser.add_argument(
        "--moz-fetches-dir", metavar="DIR", help="Directory holding fetched toolchains"
    )
    parser.add_argument(
        "--rewrite-sanitizer-dylib",
        action="store_true",
        help="Rewrite sanitizer runtime dylib paths in the staged tree",
    )
    parser.add_argument(
        "--installer", action="store_true", help="Also build the Windows installer"
    )
    parser.add_argument(
        "--stub-installer", action="store_true", help="Also build the stub installer"
    )
    parser.add_argument(
        "--source-repo-header", metavar="PATH", help="Generated source-repo.h"
    )
    return parser


def main(argv: list[str]) -> int:
    args, package_extra_args = _parser().parse_known_args(argv)
    return package_application(package_extra_args=package_extra_args, **vars(args))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
