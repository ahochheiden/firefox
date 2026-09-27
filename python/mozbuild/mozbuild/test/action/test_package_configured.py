# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import contextlib
import os
import unittest
from unittest import mock

import mozunit

from mozbuild.action import package_configured
from mozbuild.packaging import package_options

_SUBSTS = {
    "MOZ_BUILD_APP": "browser",
    "MOZ_PKG_APPNAME": "firefox",
    "MOZ_PKG_VERSION": "156.0a1",
    "MOZ_PKG_PLATFORM": "linux-x86_64",
    "PKG_SUFFIX": ".tar.xz",
    "MOZ_PKG_DIR_DEFAULT": "firefox",
    "JSSHELL_NAME": "jsshell-linux-x86_64.zip",
    "MOZ_PKG_BINPATH": "bin",
    "MOZ_PKG_RESPATH": "bin",
    "MOZ_PACKAGER_FORMAT": "omni",
    "MOZ_PKG_FORMAT": "XZ",
    "MOZ_PKG_MANIFEST": "/src/browser/installer/package-manifest.in",
    "MOZ_PKG_REMOVALS": "/src/browser/installer/removed-files.in",
    "MOZ_PKG_ALLOWED_DUPES": ["/src/browser/installer/allowed-dupes.mn"],
    "MOZ_CHROME_LOCALE_ENTRIES": ["@RESPATH@/browser/chrome/"],
    "PACKAGE_MANIFEST_DEFINES": "-DMOZ_APP_NAME=firefox",
    "PACKAGE_TOOLKIT_DEFINES": "-DDLL_SUFFIX=.so",
    "ACDEFINES": "-DXP_LINUX=1",
    "NO_PKG_FILES": ["core", "nsinstall"],
    "MOZ_PACKAGER_MINIFY": "1",
    "JAR_COMPRESSION": "none",
    "TAR": "tar",
    "OS_ARCH": "Linux",
    "OS_TARGET": "Linux",
    "MOZ_WIDGET_TOOLKIT": "gtk",
    "MOZ_AUTOMATION": "",
    "MOZ_UI_LOCALE": "en-US",
}


@contextlib.contextmanager
def _patched(environ=None):
    with contextlib.ExitStack() as es:
        es.enter_context(
            mock.patch.object(package_configured.buildconfig, "substs", _SUBSTS)
        )
        es.enter_context(
            mock.patch.object(package_configured.buildconfig, "topobjdir", "/obj")
        )
        es.enter_context(mock.patch.dict(os.environ, environ or {}, clear=True))
        yield es.enter_context(
            mock.patch.object(package_configured, "package_application", return_value=0)
        )


class TestPackageConfigured(unittest.TestCase):
    def test_defaults(self):
        with _patched() as package_application:
            rc = package_configured.main([])
            expected = package_options(
                substs=_SUBSTS, topobjdir="/obj", environ=os.environ
            )

        self.assertEqual(rc, 0)
        self.assertEqual(package_application.call_args.kwargs, expected)
        self.assertEqual(expected["mode"], "package")
        self.assertEqual(expected["ab_cd"], "en-US")

    def test_mode_and_locale(self):
        with _patched({"MOZ_PKG_DIR": "firefox-stage"}) as package_application:
            package_configured.main(["--mode=stage", "--ab-cd=multi"])
            expected = package_options(
                substs=_SUBSTS,
                topobjdir="/obj",
                environ=os.environ,
                mode="stage",
                ab_cd="multi",
            )

        self.assertEqual(package_application.call_args.kwargs, expected)
        self.assertEqual(expected["pkg_dir"], "firefox-stage")

    def test_failure(self):
        with _patched() as package_application:
            package_application.return_value = 3
            self.assertEqual(package_configured.main(["--mode=install"]), 3)


class TestPackageOptions(unittest.TestCase):
    def _options(self, substs=None, environ=None, **kwargs):
        return package_options(
            substs=substs if substs is not None else _SUBSTS,
            topobjdir="/obj",
            environ=environ or {},
            **kwargs,
        )

    def test_options(self):
        options = self._options()

        self.assertEqual(options["mode"], "package")
        self.assertEqual(
            options["manifest"], "/src/browser/installer/package-manifest.in"
        )
        self.assertEqual(options["removals"], "/src/browser/installer/removed-files.in")
        self.assertIn("core", options["no_pkg_files"])
        self.assertEqual(
            options["allowed_dupes"], ["/src/browser/installer/allowed-dupes.mn"]
        )
        self.assertTrue(options["minify"])
        self.assertEqual(options["compress"], "none")

    def test_defines(self):
        options = self._options()

        self.assertEqual(
            options["defines"],
            [
                "MOZ_APP_NAME=firefox",
                "DLL_SUFFIX=.so",
                "LPROJ_ROOT=en",
                "XP_LINUX=1",
                "AB_CD=en-US",
            ],
        )

    def test_defines_not_a_define(self):
        substs = {**_SUBSTS, "PACKAGE_MANIFEST_DEFINES": "-UMOZ_APP_NAME"}

        with self.assertRaisesRegex(
            ValueError, "expected a -D define, got -UMOZ_APP_NAME"
        ):
            self._options(substs)

    def test_defines_locale(self):
        options = self._options(ab_cd="fr-CA")

        self.assertIn("LPROJ_ROOT=fr", options["defines"])
        self.assertIn("AB_CD=fr-CA", options["defines"])

    def test_automation(self):
        options = self._options()
        self.assertTrue(options["ignore_broken_symlinks"])
        self.assertFalse(options["find_dupes"])
        self.assertFalse(options["mozharness_zip"])

        options = self._options(substs={**_SUBSTS, "MOZ_AUTOMATION": "1"})
        self.assertFalse(options["ignore_broken_symlinks"])
        self.assertTrue(options["find_dupes"])
        self.assertTrue(options["mozharness_zip"])

    def test_optional_steps_override(self):
        options = self._options(
            environ={"RUN_FIND_DUPES": "1", "RUN_MOZHARNESS_ZIP": "1"}
        )

        self.assertTrue(options["find_dupes"])
        self.assertTrue(options["mozharness_zip"])

    def test_optional_steps_empty_override(self):
        options = self._options(
            substs={**_SUBSTS, "MOZ_AUTOMATION": "1"},
            environ={"RUN_FIND_DUPES": "", "RUN_MOZHARNESS_ZIP": ""},
        )

        self.assertFalse(options["find_dupes"])
        self.assertFalse(options["mozharness_zip"])

    def test_fatal_warnings(self):
        options = self._options()
        self.assertTrue(options["ignore_errors"])

        options = self._options(substs={**_SUBSTS, "MOZ_PKG_FATAL_WARNINGS": "1"})
        self.assertFalse(options["ignore_errors"])

    def test_locale_default(self):
        options = self._options(substs={**_SUBSTS, "MOZ_UI_LOCALE": "de"})

        self.assertEqual(options["ab_cd"], "de")
        self.assertIn("AB_CD=de", options["defines"])
        self.assertIn("LPROJ_ROOT=de", options["defines"])

    def test_locale_explicit(self):
        options = self._options(
            substs={**_SUBSTS, "MOZ_UI_LOCALE": "de"}, ab_cd="multi"
        )

        self.assertEqual(options["ab_cd"], "multi")

    def test_locales_environment(self):
        options = self._options(environ={"MOZ_CHROME_MULTILOCALE": "de fr ja"})

        self.assertEqual(options["locales"], ["de", "fr", "ja"])

    def test_locales_explicit(self):
        options = self._options(
            environ={"MOZ_CHROME_MULTILOCALE": "de fr"}, locales=["ja"]
        )

        self.assertEqual(options["locales"], ["ja"])

    def test_destdir_environment(self):
        options = self._options()
        self.assertEqual(options["destdir"], "")

        options = self._options(environ={"DESTDIR": "/tmp/stage"})
        self.assertEqual(options["destdir"], "/tmp/stage")

    def test_pkg_dir_environment(self):
        options = self._options()
        self.assertIsNone(options["pkg_dir"])

        options = self._options(environ={"MOZ_PKG_DIR": "custom"})
        self.assertEqual(options["pkg_dir"], "custom")

    def test_simple_package_name_environment(self):
        options = self._options()
        self.assertIsNone(options["simple_package_name"])

        options = self._options(environ={"MOZ_SIMPLE_PACKAGE_NAME": "target"})
        self.assertEqual(options["simple_package_name"], "target")

    def test_no_pkg_files_no_manifest(self):
        substs = {**_SUBSTS}
        del substs["MOZ_PKG_MANIFEST"]
        options = self._options(substs=substs)

        self.assertIn("ssltunnel*", options["no_pkg_files"])
        self.assertNotIn("SmokeDMD", options["no_pkg_files"])

    def test_no_pkg_files_dmd(self):
        options = self._options(substs={**_SUBSTS, "MOZ_DMD": "1"})

        self.assertIn("SmokeDMD", options["no_pkg_files"])
        self.assertNotIn("ssltunnel*", options["no_pkg_files"])

    def test_langpack(self):
        options = self._options()
        self.assertFalse(options["langpack"])

        options = self._options(
            substs={
                **_SUBSTS,
                "MOZ_PKG_LANGPACK_LOCALES_DIR": "browser/locales",
                "BUILD_BACKENDS": ["RecursiveMake"],
            }
        )
        self.assertTrue(options["langpack"])

    def test_langpack_artifact_build(self):
        options = self._options(
            substs={
                **_SUBSTS,
                "MOZ_PKG_LANGPACK_LOCALES_DIR": "browser/locales",
                "BUILD_BACKENDS": ["FasterMake+RecursiveMake"],
            }
        )
        self.assertFalse(options["langpack"])

    def test_no_pkg_files(self):
        options = self._options()

        self.assertNotIn("ssltunnel*", options["no_pkg_files"])
        self.assertNotIn("SmokeDMD", options["no_pkg_files"])
        self.assertIn("core", options["no_pkg_files"])


if __name__ == "__main__":
    mozunit.main()
