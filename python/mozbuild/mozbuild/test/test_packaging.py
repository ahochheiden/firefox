# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import mozunit

from mozbuild.packaging import (
    NO_PKG_FILES,
    PackagePaths,
    all_locales,
    jsshell_name,
    langpack_basename,
    multilocale_content,
    no_pkg_files,
    package_basename,
    quote_defines,
)

_SUBSTS = {
    "MOZ_PKG_APPNAME": "firefox",
    "MOZ_PKG_VERSION": "156.0a1",
    "MOZ_PKG_PLATFORM": "win64",
    "PKG_SUFFIX": ".zip",
    "MOZ_PKG_DIR_DEFAULT": "firefox",
    "JSSHELL_NAME": "jsshell-win64.zip",
    "OS_ARCH": "WINNT",
    "OS_TARGET": "WINNT",
}


def test_no_pkg_files_with_manifest():
    files = no_pkg_files(has_manifest=True, dmd=False)
    assert files == NO_PKG_FILES
    assert "ssltunnel*" not in files
    assert "SmokeDMD" not in files


def test_no_pkg_files_without_manifest_excludes_ssltunnel():
    files = no_pkg_files(has_manifest=False, dmd=False)
    assert files[: len(NO_PKG_FILES)] == NO_PKG_FILES
    assert files[len(NO_PKG_FILES) :] == ("ssltunnel*",)


def test_no_pkg_files_with_dmd():
    files = no_pkg_files(has_manifest=True, dmd=True)
    assert files[len(NO_PKG_FILES) :] == ("SmokeDMD",)


def test_no_pkg_files_both():
    files = no_pkg_files(has_manifest=False, dmd=True)
    assert files[len(NO_PKG_FILES) :] == ("ssltunnel*", "SmokeDMD")


def test_no_pkg_files_does_not_mutate_the_shared_tuple():
    no_pkg_files(has_manifest=False, dmd=True)
    assert "ssltunnel*" not in NO_PKG_FILES
    assert "SmokeDMD" not in NO_PKG_FILES


def test_quote_defines_preserves_order():
    assert quote_defines(["-DA=1", "-DB=2"]) == "-DA=1 -DB=2"


def test_quote_defines_leaves_plain_values_unquoted():
    assert quote_defines(["-DMOZ_APP_NAME=firefox"]) == "-DMOZ_APP_NAME=firefox"


def test_quote_defines_keeps_values_with_spaces_as_one_argument():
    import shlex

    quoted = quote_defines(["-DMOZ_APP_DISPLAYNAME=Firefox Nightly"])
    assert shlex.split(quoted) == ["-DMOZ_APP_DISPLAYNAME=Firefox Nightly"]


def test_quote_defines_empty_value():
    assert quote_defines(["-DJAREXT="]) == "-DJAREXT="


def test_quote_defines_empty_list():
    assert quote_defines([]) == ""


def test_package_basename():
    assert (
        package_basename(
            appname="firefox", version="145.0a1", ab_cd="en-US", platform="linux-x86_64"
        )
        == "firefox-145.0a1.en-US.linux-x86_64"
    )


def test_package_basename_locale():
    assert (
        package_basename(
            appname="firefox", version="145.0a1", ab_cd="de", platform="win64"
        )
        == "firefox-145.0a1.de.win64"
    )


def test_package_basename_simple_name_wins():
    assert (
        package_basename(
            appname="firefox",
            version="145.0a1",
            ab_cd="en-US",
            platform="win64",
            simple_name="target",
        )
        == "target"
    )


def test_langpack_basename():
    assert (
        langpack_basename(appname="firefox", version="145.0a1", ab_cd="de")
        == "firefox-145.0a1.de.langpack"
    )


def test_langpack_basename_simple_name():
    assert (
        langpack_basename(
            appname="firefox", version="145.0a1", ab_cd="de", simple_name="target"
        )
        == "target.langpack"
    )


def test_jsshell_name():
    assert jsshell_name(platform="linux-x86_64") == "jsshell-linux-x86_64.zip"


def test_jsshell_name_simple_name():
    assert jsshell_name(platform="win64", simple_name="target") == "target.jsshell.zip"


def test_package_paths_locale():
    paths = PackagePaths(substs=_SUBSTS, dist_dir="/obj/dist", ab_cd="de")

    assert paths.basename == "firefox-156.0a1.de.win64"
    assert paths.package == "firefox-156.0a1.de.win64.zip"
    assert paths.pkg_dir == "firefox"


def test_package_paths_pkg_dir():
    paths = PackagePaths(
        substs=_SUBSTS, dist_dir="/obj/dist", pkg_dir="snap/source/usr/lib/firefox"
    )

    assert paths.pkg_dir == "snap/source/usr/lib/firefox"


def test_package_paths_metadata():
    paths = PackagePaths(substs=_SUBSTS, dist_dir="/obj/dist")

    assert paths.sourcestamp_file == "/obj/dist/firefox-156.0a1.en-US.win64.txt"
    assert paths.buildinfo_file == "/obj/dist/firefox-156.0a1.en-US.win64.json"
    assert paths.buildhub_json == "/obj/dist/buildhub.json"
    assert (
        paths.buildid_info_txt_file == "/obj/dist/firefox-156.0a1.en-US.win64_info.txt"
    )
    assert paths.mozinfo_file == "/obj/dist/firefox-156.0a1.en-US.win64.mozinfo.json"
    assert paths.jsshell == "/obj/dist/jsshell-win64.zip"


def test_package_paths_pkg_path():
    paths = PackagePaths(substs=_SUBSTS, dist_dir="/obj/dist", pkg_path="win64/")

    assert paths.pkg_output_dir == "/obj/dist/win64/"
    assert paths.package == "win64/firefox-156.0a1.en-US.win64.zip"
    assert paths.sourcestamp_file == "/obj/dist/win64/firefox-156.0a1.en-US.win64.txt"
    assert (
        paths.symbol_archive_basename
        == "firefox-156.0a1.en-US.win64.crashreporter-symbols"
    )


def test_package_paths_pkg_path_without_slash():
    paths = PackagePaths(substs=_SUBSTS, dist_dir="/obj/dist", pkg_path="win64")

    assert paths.package == "win64/firefox-156.0a1.en-US.win64.zip"


def test_package_paths_installer():
    paths = PackagePaths(substs=_SUBSTS, dist_dir="/obj/dist")

    assert paths.installer_basename == "firefox-156.0a1.en-US.win64.installer"
    assert (
        paths.installer_package == "/obj/dist/firefox-156.0a1.en-US.win64.installer.exe"
    )
    assert paths.stub_installer_basename == "firefox-156.0a1.en-US.win64.installer-stub"

    elsewhere = PackagePaths(
        substs={**_SUBSTS, "OS_TARGET": "Linux"}, dist_dir="/obj/dist"
    )

    assert elsewhere.installer_package is None


def test_package_paths_update_framework_archive():
    paths = PackagePaths(substs=_SUBSTS, dist_dir="/obj/dist")

    assert paths.update_framework_artifacts_archive_basename is None

    mac = PackagePaths(substs={**_SUBSTS, "OS_ARCH": "Darwin"}, dist_dir="/obj/dist")

    assert (
        mac.update_framework_artifacts_archive_basename
        == "firefox-156.0a1.en-US.win64.update_framework_artifacts"
    )


def test_package_paths_suffix():
    paths = PackagePaths(substs=_SUBSTS, dist_dir="/obj/dist")

    assert paths.package == "firefox-156.0a1.en-US.win64.zip"


def test_package_paths_simple_name():
    paths = PackagePaths(
        substs={**_SUBSTS, "MOZ_SIMPLE_PACKAGE_NAME": "target"},
        dist_dir="/obj/dist",
    )

    assert paths.basename == "target"
    assert paths.package == "target.zip"
    assert paths.sourcestamp_file == "/obj/dist/target.txt"


def test_all_locales():
    assert all_locales(["de", "fr"]) == ["de", "fr", "en-US"]


def test_all_locales_with_en_us():
    assert all_locales(["en-US", "de"]) == ["en-US", "de"]


def test_all_locales_empty():
    assert all_locales([]) == ["en-US"]


def test_multilocale_content():
    assert multilocale_content(["de", "fr"]) == "de,fr,en-US\n"


if __name__ == "__main__":
    mozunit.main()
