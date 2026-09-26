# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

"""Helpers shared by configure and packaging."""

from __future__ import annotations

import shlex
from typing import Iterable, Mapping, Optional

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


def all_locales(locales: Iterable[str]) -> list[str]:
    locales = [*locales]
    if "en-US" not in locales:
        locales.append("en-US")
    return locales


def multilocale_content(locales: Iterable[str]) -> str:
    return ",".join(all_locales(locales)) + "\n"
