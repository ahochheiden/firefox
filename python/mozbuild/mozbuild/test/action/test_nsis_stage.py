# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import sys
import unittest
from pathlib import Path
from shutil import rmtree
from tempfile import mkdtemp
from unittest import mock

import mozunit

from mozbuild.action import nsis_stage


class TestNsisStage(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(mkdtemp())

    def tearDown(self):
        rmtree(self.tmpdir)

    def _write(self, path, content=""):
        full = self.tmpdir / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")
        return str(full)

    def test_stage_copies_wipes_and_preprocesses(self):
        installer = self._write("src/nsis/installer.nsi", "nsi")
        plugin = self._write("plugins/UAC.dll", "dll")
        defines_in = self._write("src/nsis/defines.nsi.in", "in")
        config_dir = self.tmpdir / "instgen"
        config_dir.mkdir()
        (config_dir / "stale").write_text("old", encoding="utf-8")

        calls = []

        def fake_run(argv, check=False):
            calls.append(argv)
            return mock.Mock(returncode=0)

        with mock.patch.object(
            nsis_stage.subprocess, "run", side_effect=fake_run
        ), mock.patch.object(nsis_stage.preprocessor, "main") as pp:
            rc = nsis_stage.main([
                "--config-dir",
                str(config_dir),
                "--install",
                installer,
                "--install",
                plugin,
                "--defines-in",
                defines_in,
                "--defines-out",
                str(config_dir / "defines.nsi"),
                "--topsrcdir",
                str(self.tmpdir),
                "--locale-arg",
                "browser/locales/en-US/installer",
                "--ab-cd",
                "en-US",
                "--preprocess-locale",
                "--single-file",
                "nsisstrings.properties",
                "nsisstrings.nlf",
                "--convert-utf8",
                "extensionsLocale.nsh",
                str(config_dir / "extensionsLocale.nsh"),
                "-DFOO=1",
            ])

        self.assertEqual(rc, 0)
        self.assertFalse((config_dir / "stale").exists())
        self.assertEqual(
            (config_dir / "installer.nsi").read_text(encoding="utf-8"), "nsi"
        )
        self.assertEqual((config_dir / "UAC.dll").read_text(encoding="utf-8"), "dll")

        # Unrecognized -D arguments are forwarded to the preprocessor.
        pp.assert_called_once()
        self.assertEqual(
            pp.call_args[0][0],
            [
                "-Fsubstitution",
                "-DFOO=1",
                defines_in,
                "-o",
                str(config_dir / "defines.nsi"),
            ],
        )

        ppl = str(self.tmpdir / nsis_stage._PREPROCESS_LOCALE)
        self.assertEqual(
            calls,
            [
                [
                    sys.executable,
                    ppl,
                    "--preprocess-locale",
                    str(self.tmpdir),
                    "browser/locales/en-US/installer",
                    "en-US",
                    str(config_dir),
                ],
                [
                    sys.executable,
                    ppl,
                    "--preprocess-single-file",
                    str(self.tmpdir),
                    "browser/locales/en-US/installer",
                    str(config_dir),
                    "nsisstrings.properties",
                    "nsisstrings.nlf",
                ],
                [
                    sys.executable,
                    ppl,
                    "--convert-utf8-utf16le",
                    "extensionsLocale.nsh",
                    str(config_dir / "extensionsLocale.nsh"),
                ],
            ],
        )

    def test_preprocess_failure_propagates(self):
        config_dir = self.tmpdir / "instgen"

        with mock.patch.object(
            nsis_stage.subprocess,
            "run",
            return_value=mock.Mock(returncode=4),
        ):
            rc = nsis_stage.main([
                "--config-dir",
                str(config_dir),
                "--topsrcdir",
                str(self.tmpdir),
                "--preprocess-locale",
            ])

        self.assertEqual(rc, 4)


if __name__ == "__main__":
    mozunit.main()
