# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

"""Helpers shared by configure and packaging."""

from __future__ import annotations

import os
import shlex
from typing import Iterable, Mapping, Optional, Sequence

import mozpack.path as mozpath

NO_PKG_FILES = (
    "core",
    "bsdecho",
    "js",
    "js-config",
    "jscpucfg",
    "viewer",
    "TestGtkEmbed",
    "elf-dynstr-gc",
    "mangle*",
    "maptsv*",
    "mfc*",
    "msdump*",
    "msmap*",
    "nm2tsv*",
    "res/samples",
    "res/throbber",
    "shlibsign*",
    "certutil*",
    "pk12util*",
    "BadCertAndPinningServer*",
    "DelegatedCredentialsServer*",
    "EncryptedClientHelloServer*",
    "FaultyServer*",
    "OCSPStaplingServer*",
    "SanctionsTestServer*",
    "GenerateOCSPResponse*",
    "chrome/chrome.rdf",
    "chrome/app-chrome.manifest",
    "chrome/overlayinfo",
    "components/compreg.dat",
    "components/xpti.dat",
    "content_unit_tests",
    "necko_unit_tests",
    "*.dSYM",
)


def no_pkg_files(*, has_manifest: bool, dmd: bool) -> tuple:
    files = NO_PKG_FILES
    if not has_manifest:
        files += ("ssltunnel*",)
    if dmd:
        files += ("SmokeDMD",)
    return files


def quote_defines(defines: Iterable[str]) -> str:
    return " ".join(shlex.quote(d) for d in defines)


def package_basename(
    *,
    appname: str,
    version: str,
    ab_cd: str,
    platform: str,
    simple_name: Optional[str] = None,
) -> str:
    if simple_name:
        return simple_name
    return f"{appname}-{version}.{ab_cd}.{platform}"


def langpack_basename(
    *,
    appname: str,
    version: str,
    ab_cd: str,
    simple_name: Optional[str] = None,
) -> str:
    if simple_name:
        return f"{simple_name}.langpack"
    return f"{appname}-{version}.{ab_cd}.langpack"


def jsshell_name(*, platform: str, simple_name: Optional[str] = None) -> str:
    if simple_name:
        return f"{simple_name}.jsshell.zip"
    return f"jsshell-{platform}.zip"


class PackagePaths:
    def __init__(
        self,
        *,
        substs: Mapping[str, str],
        dist_dir: str,
        ab_cd: str = "en-US",
        pkg_dir: Optional[str] = None,
        pkg_path: str = "",
        simple_name: Optional[str] = None,
    ):
        simple_name = simple_name or substs.get("MOZ_SIMPLE_PACKAGE_NAME")
        basename = package_basename(
            appname=substs["MOZ_PKG_APPNAME"],
            version=substs["MOZ_PKG_VERSION"],
            ab_cd=ab_cd,
            platform=substs["MOZ_PKG_PLATFORM"],
            simple_name=simple_name,
        )
        installer_basename = f"{basename}.installer"
        pkg_output_dir = mozpath.join(dist_dir, pkg_path)
        darwin = substs.get("OS_ARCH") == "Darwin"

        self.dist_dir = dist_dir
        self.pkg_output_dir = pkg_output_dir
        self.pkg_dir = pkg_dir or substs["MOZ_PKG_DIR_DEFAULT"]
        self.basename = basename
        self.package = mozpath.join(pkg_path, f"{basename}{substs['PKG_SUFFIX']}")
        self.installer_package = (
            mozpath.join(pkg_output_dir, f"{installer_basename}.exe")
            if substs.get("OS_TARGET") == "WINNT"
            else None
        )
        self.installer_basename = installer_basename
        self.stub_installer_basename = f"{basename}.installer-stub"
        self.checksums_file = mozpath.join(pkg_output_dir, f"{basename}.checksums")
        self.langpack = (
            f"{substs.get('PKG_LANGPACK_PATH', '')}"
            + langpack_basename(
                appname=substs["MOZ_PKG_APPNAME"],
                version=substs["MOZ_PKG_VERSION"],
                ab_cd=ab_cd,
                simple_name=simple_name,
            )
            + ".xpi"
        )
        self.generated_source_file_package = f"{basename}.generated-files.tar.gz"
        self.test_packages_file = mozpath.join(
            pkg_output_dir, f"{basename}.test_packages.json"
        )
        self.symbol_archive_basename = f"{basename}.crashreporter-symbols"
        self.symbol_full_archive_basename = f"{basename}.crashreporter-symbols-full"
        self.code_coverage_archive_basename = f"{basename}.code-coverage-gcno"
        self.mozsearch_archive_basename = f"{basename}.mozsearch-index"
        self.mozsearch_includemap_basename = f"{basename}.mozsearch-distinclude"
        self.mozsearch_scip_index_basename = f"{basename}.mozsearch-scip-index"
        self.mozsearch_java_index_basename = f"{basename}.mozsearch-java-index"
        self.mozharness_package = "mozharness.zip"
        self.libmegazord_artifacts_archive_basename = (
            f"{basename}.libmegazord_so_artifacts"
        )
        self.xpt_artifacts_archive_basename = f"{basename}.xpt_artifacts"
        self.update_framework_artifacts_archive_basename = (
            f"{basename}.update_framework_artifacts" if darwin else None
        )
        self.sourcestamp_file = mozpath.join(pkg_output_dir, f"{basename}.txt")
        self.buildinfo_file = mozpath.join(pkg_output_dir, f"{basename}.json")
        self.buildhub_json = mozpath.join(pkg_output_dir, "buildhub.json")
        self.buildid_info_txt_file = mozpath.join(
            pkg_output_dir, f"{basename}_info.txt"
        )
        self.mozinfo_file = mozpath.join(pkg_output_dir, f"{basename}.mozinfo.json")
        self.jsshell = mozpath.join(dist_dir, substs["JSSHELL_NAME"])


def files_to_upload(
    *,
    substs: Mapping[str, str],
    paths: PackagePaths,
    topsrcdir: str,
    topobjdir: str,
    ab_cd: str = "en-US",
    extra_files: Sequence[str] = (),
) -> list[str]:
    dist = paths.dist_dir
    out = paths.pkg_output_dir
    app = substs["MOZ_BUILD_APP"]
    installer_objdir = mozpath.join(topobjdir, app, "installer", "windows")
    android = app == "mobile/android"

    candidates = [
        mozpath.join(dist, paths.package),
        paths.installer_package,
        mozpath.join(dist, paths.langpack),
        mozpath.join(out, paths.mozharness_package),
        mozpath.join(out, f"{paths.symbol_archive_basename}.zip"),
        mozpath.join(out, paths.generated_source_file_package),
        paths.sourcestamp_file,
        paths.buildinfo_file,
        paths.buildhub_json,
        paths.buildid_info_txt_file,
        paths.mozinfo_file,
        paths.test_packages_file,
        paths.jsshell,
        mozpath.join(out, f"{paths.symbol_full_archive_basename}.tar.zst"),
        mozpath.join(installer_objdir, "instgen", "setup.exe"),
        mozpath.join(installer_objdir, "instgen", "setup-stub.exe"),
        mozpath.join(topsrcdir, "toolchains.json"),
        mozpath.join(topobjdir, "sbom.json"),
        mozpath.join(topobjdir, "config.status"),
        mozpath.join(topobjdir, "licenses.json"),
    ]

    candidates += [mozpath.join(dist, extra) for extra in extra_files]

    if ab_cd in ("", "en-US"):
        suffix = ".exe" if substs.get("OS_TARGET") == "WINNT" else ""
        candidates += [
            mozpath.join(dist, "host", "bin", f"mar{suffix}"),
            mozpath.join(dist, "host", "bin", f"mbsdiff{suffix}"),
        ]
    else:
        candidates += [
            mozpath.join(installer_objdir, "l10ngen", "setup.exe"),
            mozpath.join(installer_objdir, "l10ngen", "setup-stub.exe"),
        ]

    if substs.get("MOZ_CODE_COVERAGE"):
        candidates += [
            mozpath.join(out, f"{paths.code_coverage_archive_basename}.zip"),
            mozpath.join(topobjdir, "chrome-map.json"),
        ]

    if substs.get("ENABLE_MOZSEARCH_PLUGIN"):
        candidates += [
            mozpath.join(topobjdir, "chrome-map.json"),
            mozpath.join(out, f"{paths.mozsearch_archive_basename}.zip"),
            mozpath.join(out, f"{paths.mozsearch_scip_index_basename}.zip"),
            mozpath.join(out, f"{paths.mozsearch_includemap_basename}.map"),
        ]
        if android:
            candidates.append(
                mozpath.join(out, f"{paths.mozsearch_java_index_basename}.zip")
            )

    if substs.get("MOZ_STUB_INSTALLER"):
        candidates.append(mozpath.join(out, f"{paths.stub_installer_basename}.exe"))

    if substs.get("MOZ_APPSERVICES_IN_TREE"):
        candidates.append(
            mozpath.join(out, f"{paths.libmegazord_artifacts_archive_basename}.zip")
        )

    candidates.append(mozpath.join(out, f"{paths.xpt_artifacts_archive_basename}.zip"))

    if paths.update_framework_artifacts_archive_basename:
        candidates.append(
            mozpath.join(
                out, f"{paths.update_framework_artifacts_archive_basename}.zip"
            )
        )

    return [f for f in dict.fromkeys(candidates) if f and os.path.exists(f)]


def lproj_root(*, ab_cd: str, cocoa: bool) -> str:
    if cocoa and ab_cd == "zh-TW":
        return ab_cd.replace("-", "_")
    return ab_cd.split("-", maxsplit=1)[0]


def _bare_define(token: str) -> str:
    if not token.startswith("-D"):
        raise ValueError(f"expected a -D define, got {token}")
    return token[2:]


def package_defines(*, substs: Mapping[str, str], ab_cd: str = "en-US") -> list[str]:
    cocoa = substs.get("MOZ_WIDGET_TOOLKIT") == "cocoa"
    tokens = shlex.split(substs.get("PACKAGE_MANIFEST_DEFINES") or "")
    tokens += shlex.split(substs.get("PACKAGE_TOOLKIT_DEFINES") or "")
    tokens.append(f"-DLPROJ_ROOT={lproj_root(ab_cd=ab_cd, cocoa=cocoa)}")
    # ACDEFINES stores dollar signs doubled. Restore them before preprocessing.
    tokens += shlex.split((substs.get("ACDEFINES") or "").replace("$$", "$"))
    tokens.append(f"-DAB_CD={ab_cd}")
    return [_bare_define(token) for token in tokens]


def _optional_step(environ: Mapping[str, str], name: str, automation: bool) -> bool:
    """An environment variable set to anything turns a step on, one set to
    nothing turns it off, and one left unset follows automation."""
    value = environ.get(name)
    return bool(value) if value is not None else automation


def package_options(
    *,
    substs: Mapping[str, str],
    topobjdir: str,
    environ: Mapping[str, str],
    ab_cd: Optional[str] = None,
    locales: Optional[Sequence[str]] = None,
    mode: str = "package",
) -> dict:
    ab_cd = ab_cd or substs.get("MOZ_UI_LOCALE") or "en-US"
    if locales is None:
        locales = (environ.get("MOZ_CHROME_MULTILOCALE") or "").split()
    automation = bool(substs.get("MOZ_AUTOMATION"))
    omni = substs.get("MOZ_PACKAGER_FORMAT") == "omni"
    jarlog = substs.get("PGO_JARLOG_PATH") or mozpath.join(
        topobjdir, "jarlog", f"{ab_cd}.log"
    )
    sanitizer = any(substs.get(name) for name in ("MOZ_ASAN", "LIBFUZZER", "MOZ_UBSAN"))
    cocoa = substs.get("MOZ_WIDGET_TOOLKIT") == "cocoa"

    return dict(
        mode=mode,
        ab_cd=ab_cd,
        pkg_dir=environ.get("MOZ_PKG_DIR") or None,
        simple_package_name=environ.get("MOZ_SIMPLE_PACKAGE_NAME") or None,
        locales=locales,
        locale_entries=substs.get("MOZ_CHROME_LOCALE_ENTRIES") or [],
        defines=package_defines(substs=substs, ab_cd=ab_cd),
        manifest=substs.get("MOZ_PKG_MANIFEST") or None,
        removals=substs.get("MOZ_PKG_REMOVALS") or None,
        non_resources=(
            shlex.split(substs.get("NON_OMNIJAR_FILES") or "") if omni else []
        ),
        no_pkg_files=list(
            no_pkg_files(
                has_manifest=bool(substs.get("MOZ_PKG_MANIFEST")),
                dmd=bool(substs.get("MOZ_DMD")),
            )
        ),
        minify=bool(substs.get("MOZ_PACKAGER_MINIFY")),
        minify_js=bool(substs.get("MOZ_PACKAGER_MINIFY_JS")),
        minify_pdfjs=bool(substs.get("MOZ_PACKAGER_MINIFY_PDFJS")),
        jarlog=jarlog if os.path.exists(jarlog) else None,
        compress=substs.get("JAR_COMPRESSION") or "deflate",
        ignore_errors=not substs.get("MOZ_PKG_FATAL_WARNINGS"),
        ignore_broken_symlinks=not automation,
        find_dupes=_optional_step(environ, "RUN_FIND_DUPES", automation),
        allowed_dupes=substs.get("MOZ_PKG_ALLOWED_DUPES") or [],
        mozharness_zip=_optional_step(environ, "RUN_MOZHARNESS_ZIP", automation),
        jsshell=bool(substs.get("MOZ_PACKAGE_JSSHELL")),
        automation=automation,
        artifact_build_symbols=substs.get("MOZ_ARTIFACT_BUILD_SYMBOLS") or None,
        upload_dir=environ.get("UPLOAD_DIR"),
        code_coverage=bool(substs.get("MOZ_CODE_COVERAGE")),
        mozsearch=bool(substs.get("ENABLE_MOZSEARCH_PLUGIN")),
        moz_fetches_dir=environ.get("MOZ_FETCHES_DIR"),
        rewrite_sanitizer_dylib=(
            cocoa and substs.get("OS_ARCH") == "Darwin" and sanitizer
        ),
        installer=substs.get("OS_ARCH") == "WINNT",
        stub_installer=bool(substs.get("MOZ_STUB_INSTALLER")),
        langpack=bool(substs.get("MOZ_PKG_LANGPACK_LOCALES_DIR"))
        and "FasterMake+RecursiveMake" not in (substs.get("BUILD_BACKENDS") or []),
        destdir=environ.get("DESTDIR", ""),
        source_repo_header=(
            mozpath.join(topobjdir, "source-repo.h")
            if substs.get("MOZ_INCLUDE_SOURCE_INFO")
            else None
        ),
        package_extra_args=shlex.split(substs.get("MOZ_PACKAGE_EXTRA_ARGS") or ""),
    )


def all_locales(locales: Iterable[str]) -> list[str]:
    locales = [*locales]
    if "en-US" not in locales:
        locales.append("en-US")
    return locales


def multilocale_content(locales: Iterable[str]) -> str:
    return ",".join(all_locales(locales)) + "\n"
