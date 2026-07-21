# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import contextlib
import unittest
from pathlib import Path
from unittest import mock

import mozunit

from mozbuild.action import l10n_repackage
from mozbuild.nsis import (
    NSIS_BRANDING_FILES,
    NSIS_CUSTOM_PLUGINS,
    NSIS_TOOLKIT_FILES,
    installer_files,
)

_SUBSTS = {
    "MOZ_BRANDING_DIRECTORY": "browser/branding/nightly",
    # Exercise shell quoting for a define containing spaces.
    "NSIS_INSTALLER_DEFINES": "-DAPP_VERSION=1.0 '-DMOZ_APP_DISPLAYNAME=Firefox Nightly'",
    # Exercise the doubled dollar signs that ACDEFINES uses.
    "ACDEFINES": "-DHAS_DOLLAR=a$$b",
    "MAKENSISU": "makensis",
    "MAKENSISU_FLAGS": "-nocd",
}


class TestBuildUninstaller(unittest.TestCase):
    def _run(self, locale="de", stage_rc=0, build_rc=0):
        with contextlib.ExitStack() as es:
            es.enter_context(
                mock.patch.object(l10n_repackage.buildconfig, "substs", _SUBSTS)
            )
            es.enter_context(
                mock.patch.object(l10n_repackage.buildconfig, "topsrcdir", "/src")
            )
            es.enter_context(
                mock.patch.object(l10n_repackage.buildconfig, "topobjdir", "/obj")
            )
            stage = es.enter_context(
                mock.patch.object(
                    l10n_repackage.nsis_stage, "nsis_stage", return_value=stage_rc
                )
            )
            build = es.enter_context(
                mock.patch.object(
                    l10n_repackage.nsis_build, "nsis_build", return_value=build_rc
                )
            )
            rc = l10n_repackage._build_uninstaller(
                installer_dir=Path("/obj/browser/installer/windows"),
                locale=locale,
                real_locale_mergedir=Path("/obj/merged"),
                stagedist=Path("/obj/stage/dist"),
            )
        return rc, stage, build

    def test_preprocessor_args(self):
        rc, stage, _ = self._run(locale="de")

        self.assertEqual(rc, 0)
        args = stage.call_args.kwargs["preprocessor_args"]

        # Preserve define precedence when composing the preprocessor arguments.
        self.assertEqual(
            args,
            [
                "-DAPP_VERSION=1.0",
                "-DMOZ_APP_DISPLAYNAME=Firefox Nightly",
                "-DHAS_DOLLAR=a$b",
                "-DAB_CD=de",
                "-DTOPOBJDIR=/obj",
            ],
        )

    def test_builds_uninstaller_from_staged_dir(self):
        rc, stage, build = self._run(locale="de")

        self.assertEqual(rc, 0)
        stage.assert_called_once()
        build.assert_called_once()

        src = Path("/src")
        installer_srcdir = src / "browser/installer/windows"
        branding = src / "browser/branding/nightly"
        toolkit_nsis = src / "toolkit/mozapps/installer/windows/nsis"
        plugins = src / "other-licenses/nsis/Plugins"
        config_dir = Path("/obj/browser/installer/windows") / "l10ngen"

        stage_kwargs = stage.call_args.kwargs
        self.assertEqual(stage_kwargs["config_dir"], config_dir)
        self.assertEqual(
            stage_kwargs["installs"],
            [
                str(installer_srcdir / f)
                for f in installer_files(maintenance_service=False)
            ]
            + [str(branding / f) for f in NSIS_BRANDING_FILES]
            + [str(toolkit_nsis / f) for f in NSIS_TOOLKIT_FILES]
            + [str(plugins / f) for f in NSIS_CUSTOM_PLUGINS],
        )
        self.assertEqual(
            stage_kwargs["defines_in"],
            str(installer_srcdir / "nsis" / "defines.nsi.in"),
        )
        self.assertEqual(stage_kwargs["defines_out"], str(config_dir / "defines.nsi"))
        self.assertEqual(stage_kwargs["topsrcdir"], src)
        self.assertEqual(stage_kwargs["ab_cd"], "de")
        self.assertEqual(
            stage_kwargs["locale_args"],
            [
                f"--l10n-dir={Path('/obj/merged')}/browser/installer",
                f"--l10n-dir={src}/browser/locales/en-US/installer",
            ],
        )
        self.assertTrue(stage_kwargs["preprocess_locale"])
        self.assertEqual(stage_kwargs["single_files"], [])
        self.assertEqual(stage_kwargs["convert_utf8"], [])

        build_kwargs = build.call_args.kwargs
        self.assertEqual(build_kwargs["config_dir"], config_dir)
        self.assertEqual(build_kwargs["nsi"], "uninstaller.nsi")
        self.assertEqual(build_kwargs["makensis"], "makensis")
        self.assertEqual(build_kwargs["makensis_flags"], ["-nocd"])
        self.assertEqual(build_kwargs["produced"], "helper.exe")
        self.assertEqual(
            build_kwargs["output"],
            str(Path("/obj/stage/dist") / "uninstall" / "helper.exe"),
        )

    def test_stage_failure_skips_build(self):
        rc, _, build = self._run(stage_rc=7)

        self.assertEqual(rc, 7)
        build.assert_not_called()

    def test_build_failure_propagates(self):
        rc, stage, build = self._run(build_rc=9)

        self.assertEqual(rc, 9)
        stage.assert_called_once()
        build.assert_called_once()


if __name__ == "__main__":
    mozunit.main()
