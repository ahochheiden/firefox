# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import contextlib
import os
import tempfile
import unittest
from unittest import mock

import mozpack.path as mozpath
import mozunit

from mozbuild.action import package_application

_WIN_SUBSTS = {
    "MOZ_BUILD_APP": "browser",
    "MOZ_PKG_APPNAME": "firefox",
    "MOZ_PKG_VERSION": "156.0a1",
    "MOZ_PKG_PLATFORM": "win64",
    "PKG_SUFFIX": ".zip",
    "MOZ_PKG_DIR_DEFAULT": "firefox",
    "JSSHELL_NAME": "jsshell-win64.zip",
    "MOZ_PKG_BINPATH": "bin",
    "MOZ_PKG_RESPATH": "bin",
    "MOZ_PACKAGER_FORMAT": "omni",
    "MOZ_PKG_FORMAT": "ZIP",
    "TAR": "tar",
    "OS_ARCH": "WINNT",
    "OS_TARGET": "WINNT",
    "MOZ_WIDGET_TOOLKIT": "windows",
    "COMPILE_ENVIRONMENT": "1",
    "NSIS_SFX_MODULE": "other-licenses/7zstub/firefox/7zSD.Win32.sfx",
    "MAKENSISU": "makensis",
    "MAKENSISU_FLAGS": "-nocd",
}

_MAC_SUBSTS = {
    **_WIN_SUBSTS,
    "MOZ_PKG_PLATFORM": "mac",
    "PKG_SUFFIX": ".dmg",
    "MOZ_PKG_FORMAT": "DMG",
    "MOZ_PKG_BINPATH": "Nightly.app/Contents/MacOS",
    "MOZ_PKG_RESPATH": "Nightly.app/Contents/Resources",
    "MOZ_MACBUNDLE_NAME": "Nightly.app",
    "OS_ARCH": "Darwin",
    "OS_TARGET": "Darwin",
    "MOZ_WIDGET_TOOLKIT": "cocoa",
}

_ANDROID_SUBSTS = {
    **_WIN_SUBSTS,
    "MOZ_BUILD_APP": "mobile/android",
    "MOZ_PKG_PLATFORM": "android-arm",
    "PKG_SUFFIX": "",
    "MOZ_PKG_FORMAT": "APK",
    "MOZ_PKG_DIR_DEFAULT": "geckoview",
    "OS_ARCH": "Linux",
    "OS_TARGET": "Android",
    "MOZ_WIDGET_TOOLKIT": "android",
}


@contextlib.contextmanager
def _patched(substs, topobjdir):
    with contextlib.ExitStack() as es:
        es.enter_context(
            mock.patch.object(package_application.buildconfig, "substs", substs)
        )
        es.enter_context(
            mock.patch.object(package_application.buildconfig, "topsrcdir", "/src")
        )
        es.enter_context(
            mock.patch.object(package_application.buildconfig, "topobjdir", topobjdir)
        )
        yield {
            "stage": es.enter_context(
                mock.patch.object(package_application, "_stage", return_value=0)
            ),
            "prepare": es.enter_context(
                mock.patch.object(
                    package_application.package_prepare,
                    "package_prepare",
                    return_value=0,
                )
            ),
            "check_symbol_archives": es.enter_context(
                mock.patch.object(
                    package_application.package_prepare,
                    "check_symbol_archives",
                    return_value=0,
                )
            ),
            "sourcestamp": es.enter_context(
                mock.patch.object(
                    package_application.make_sourcestamp_file, "main", return_value=0
                )
            ),
            "archive": es.enter_context(
                mock.patch.object(
                    package_application.package_action, "main", return_value=0
                )
            ),
            "informulate": es.enter_context(
                mock.patch.object(
                    package_application.informulate, "main", return_value=0
                )
            ),
            "copyfile": es.enter_context(
                mock.patch.object(package_application.shutil, "copyfile")
            ),
            "nsis_build": es.enter_context(
                mock.patch.object(
                    package_application.nsis_build, "nsis_build", return_value=0
                )
            ),
            "subprocess": es.enter_context(
                mock.patch.object(
                    package_application.subprocess, "call", return_value=0
                )
            ),
            "l10n_merge": es.enter_context(
                mock.patch.object(
                    package_application.l10n_merge, "main", return_value=0
                )
            ),
            "l10n_stage": es.enter_context(
                mock.patch.object(
                    package_application.l10n_stage, "main", return_value=0
                )
            ),
            "package_langpack": es.enter_context(
                mock.patch.object(
                    package_application.package_langpack, "main", return_value=0
                )
            ),
        }


class TestPackageApplication(unittest.TestCase):
    def _run(self, substs=None, **kwargs):
        substs = substs if substs is not None else _WIN_SUBSTS
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "dist"))
            with _patched(substs, tmp) as calls:
                rc = package_application.package_application(**kwargs)
            name = os.path.join(tmp, "dist", "package_name.txt")
            package_name = None
            if os.path.exists(name):
                with open(name, "rb") as f:
                    package_name = f.read()
        return rc, calls, package_name

    def test_step_order(self):
        order = []
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "dist"))
            with _patched(_WIN_SUBSTS, tmp) as calls:
                calls["stage"].side_effect = lambda **kw: order.append("stage") or 0
                calls["prepare"].side_effect = lambda **kw: order.append("prepare") or 0
                calls["sourcestamp"].side_effect = lambda *a: (
                    order.append("sourcestamp") or 0
                )
                calls["archive"].side_effect = lambda *a: order.append("archive") or 0
                calls["copyfile"].side_effect = lambda *a: order.append("mozinfo")
                calls["informulate"].side_effect = lambda *a: (
                    order.append("informulate") or 0
                )
                package_application.package_application(automation=True)

        self.assertEqual(
            order,
            ["stage", "prepare", "sourcestamp", "archive", "mozinfo", "informulate"],
        )

    def test_stage_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _patched(_WIN_SUBSTS, tmp) as calls:
                calls["stage"].return_value = 1
                rc = package_application.package_application()

        self.assertEqual(rc, 1)
        self.assertEqual(calls["prepare"].call_count, 0)

    def test_prepare_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _patched(_WIN_SUBSTS, tmp) as calls:
                calls["prepare"].return_value = 1
                rc = package_application.package_application()

        self.assertEqual(rc, 1)
        self.assertEqual(calls["sourcestamp"].call_count, 0)

    def test_locale_inputs_respath(self):
        _, calls, _ = self._run(locales=["de"], locale_entries=["@RESPATH@/chrome/"])

        kwargs = calls["stage"].call_args.kwargs
        self.assertTrue(
            kwargs["multilocale_txt"].endswith("dist/bin/res/multilocale.txt")
        )
        self.assertEqual(kwargs["base_path"], "@RESPATH@")
        self.assertTrue(
            kwargs["locale_manifest_path"].endswith(
                "browser/installer/locale-manifest.in"
            )
        )

    def test_locale_inputs_binpath_android(self):
        _, calls, _ = self._run(substs=_ANDROID_SUBSTS)

        kwargs = calls["stage"].call_args.kwargs
        self.assertEqual(kwargs["base_path"], "@BINPATH@")
        self.assertTrue(
            kwargs["locale_manifest_path"].endswith(
                "mobile/android/installer/locale-manifest.in"
            )
        )

    def test_stage_destination(self):
        _, calls, _ = self._run(manifest="package-manifest.in")

        self.assertTrue(
            calls["stage"].call_args.kwargs["destination"].endswith("/firefox")
        )

    def test_stage_destination_bundle(self):
        _, calls, _ = self._run(substs=_MAC_SUBSTS)

        self.assertTrue(
            calls["stage"]
            .call_args.kwargs["destination"]
            .endswith("firefox/Nightly.app/Contents/MacOS")
        )

    def test_stage_destination_bundle_manifest(self):
        _, calls, _ = self._run(substs=_MAC_SUBSTS, manifest="package-manifest.in")

        self.assertTrue(
            calls["stage"].call_args.kwargs["destination"].endswith("/firefox")
        )

    def test_symbol_archives(self):
        _, calls, _ = self._run(
            automation=True, artifact_build_symbols="full", upload_dir="/upload"
        )

        self.assertEqual(
            calls["prepare"].call_args.kwargs["symbol_archives"],
            [
                "/upload/firefox-156.0a1.en-US.win64.crashreporter-symbols.zip",
                "/upload/firefox-156.0a1.en-US.win64."
                "crashreporter-symbols-full.tar.zst",
            ],
        )

    def test_symbol_archives_partial(self):
        _, calls, _ = self._run(
            automation=True, artifact_build_symbols="1", upload_dir="/upload"
        )

        self.assertEqual(
            calls["prepare"].call_args.kwargs["symbol_archives"],
            ["/upload/firefox-156.0a1.en-US.win64.crashreporter-symbols.zip"],
        )

    def test_symbol_archives_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "dist"))
            with _patched(_WIN_SUBSTS, tmp) as calls:
                calls["check_symbol_archives"].return_value = 1
                rc = package_application.package_application(
                    automation=True, artifact_build_symbols="1", upload_dir="/upload"
                )

        self.assertEqual(rc, 1)
        self.assertEqual(calls["stage"].call_count, 0)

    def test_symbol_archives_no_upload_dir(self):
        rc, calls, _ = self._run(automation=True, artifact_build_symbols="1")

        self.assertEqual(rc, 1)
        self.assertEqual(calls["stage"].call_count, 0)

    def test_xpt_archive(self):
        _, calls, _ = self._run()

        archives = calls["prepare"].call_args.kwargs["archives"]
        self.assertEqual(len(archives), 1)
        self.assertTrue(archives[0][0].endswith("config/makefiles/xpidl"))
        self.assertEqual(archives[0][2], "*.xpt")

    def test_xpt_archive_artifact_build(self):
        substs = {**_WIN_SUBSTS, "MOZ_ARTIFACT_BUILDS": "1"}
        _, calls, _ = self._run(substs=substs)

        archives = calls["prepare"].call_args.kwargs["archives"]
        self.assertEqual(len(archives), 1)
        self.assertTrue(archives[0][0].endswith("dist/xpt_artifacts"))

    def test_update_framework_archive(self):
        _, calls, _ = self._run(substs=_MAC_SUBSTS)

        archives = calls["prepare"].call_args.kwargs["archives"]
        self.assertEqual(len(archives), 2)
        self.assertTrue(archives[1][0].endswith("dist/update_framework_artifacts"))
        self.assertEqual(archives[1][2], "*.framework")

    def test_archive_apk(self):
        _, calls, package_name = self._run(substs=_ANDROID_SUBSTS)

        self.assertEqual(calls["archive"].call_count, 0)
        self.assertEqual(package_name, b"firefox-156.0a1.en-US.android-arm\n")

    def test_archive(self):
        _, calls, package_name = self._run()

        argv = calls["archive"].call_args.args[0]
        self.assertEqual(argv[:2], ["--format", "ZIP"])
        self.assertIn("--pkg-dir", argv)
        self.assertEqual(argv[argv.index("--pkg-dir") + 1], "firefox")
        self.assertEqual(
            argv[argv.index("--basename") + 1], "firefox-156.0a1.en-US.win64"
        )
        self.assertEqual(package_name, b"firefox-156.0a1.en-US.win64.zip\n")

    def test_archive_extra_args(self):
        _, calls, _ = self._run(package_extra_args=["--strong-compression"])

        self.assertEqual(calls["archive"].call_args.args[0][-1], "--strong-compression")

    def test_sourcestamp_source_repo_header(self):
        _, calls, _ = self._run(source_repo_header="/obj/source-repo.h")

        argv = calls["sourcestamp"].call_args.args[0]
        self.assertEqual(argv[-2:], ["--source-repo-header", "/obj/source-repo.h"])

    def test_sourcestamp(self):
        _, calls, _ = self._run()

        self.assertNotIn("--source-repo-header", calls["sourcestamp"].call_args.args[0])

    def test_automation_artifacts_off(self):
        _, calls, _ = self._run()

        self.assertEqual(calls["copyfile"].call_count, 0)
        self.assertEqual(calls["informulate"].call_count, 0)

    def test_automation_artifacts(self):
        _, calls, _ = self._run(automation=True)

        self.assertTrue(calls["copyfile"].call_args.args[0].endswith("mozinfo.json"))
        self.assertTrue(
            calls["copyfile"]
            .call_args.args[1]
            .endswith("firefox-156.0a1.en-US.win64.mozinfo.json")
        )
        argv = calls["informulate"].call_args.args[0]
        self.assertIn("--ab-cd=en-US", argv)
        self.assertTrue(any(a.startswith("--package=") for a in argv))
        self.assertNotIn("--no-download", argv)

    def test_automation_artifacts_android(self):
        _, calls, _ = self._run(substs=_ANDROID_SUBSTS, automation=True)

        self.assertIn("--no-download", calls["informulate"].call_args.args[0])

    def test_automation_artifacts_android_fennec(self):
        substs = {**_ANDROID_SUBSTS, "MOZ_ANDROID_WITH_FENNEC": "1"}
        _, calls, _ = self._run(substs=substs, automation=True)

        self.assertNotIn("--no-download", calls["informulate"].call_args.args[0])


class TestWindowsInstaller(unittest.TestCase):
    def _run(self, substs=None, **kwargs):
        substs = substs if substs is not None else _WIN_SUBSTS
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "dist"))
            with _patched(substs, tmp) as calls:
                rc = package_application.package_application(installer=True, **kwargs)
        return rc, calls

    def test_installer(self):
        rc, calls = self._run()

        self.assertEqual(rc, 0)
        self.assertEqual(
            [call.kwargs["nsi"] for call in calls["nsis_build"].call_args_list],
            ["installer.nsi"],
        )
        self.assertTrue(
            str(calls["nsis_build"].call_args.kwargs["config_dir"]).endswith(
                os.path.join("browser", "installer", "windows", "instgen")
            )
        )
        self.assertEqual(
            calls["nsis_build"].call_args.kwargs["buildid_header"].name, "buildid.h"
        )
        argv = calls["subprocess"].call_args.args[0]
        self.assertEqual(argv[2:4], ["repackage", "installer"])
        self.assertEqual(argv[argv.index("--package-name") + 1], "firefox")

    def test_installer_stub(self):
        _, calls = self._run(stub_installer=True)

        self.assertEqual(
            [call.kwargs["nsi"] for call in calls["nsis_build"].call_args_list],
            ["installer.nsi", "stub.nsi"],
        )
        self.assertEqual(calls["subprocess"].call_count, 2)
        argv = calls["subprocess"].call_args_list[1].args[0]
        self.assertTrue(
            argv[argv.index("-o") + 1].endswith(
                "firefox-156.0a1.en-US.win64.installer-stub.exe"
            )
        )
        self.assertNotIn("--package-name", argv)

    def test_installer_automation(self):
        rc, calls = self._run(stub_installer=True, automation=True)

        self.assertEqual(rc, 0)
        self.assertEqual(
            [call.kwargs["nsi"] for call in calls["nsis_build"].call_args_list],
            ["installer.nsi", "stub.nsi"],
        )
        self.assertEqual(calls["subprocess"].call_count, 0)

    def test_installer_other_format(self):
        substs = {**_WIN_SUBSTS, "MOZ_PKG_FORMAT": "TAR"}
        _, calls = self._run(substs=substs)

        self.assertEqual(calls["nsis_build"].call_count, 0)

    def test_installer_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "dist"))
            with _patched(_WIN_SUBSTS, tmp) as calls:
                calls["nsis_build"].return_value = 4
                rc = package_application.package_application(
                    installer=True, automation=True
                )

        self.assertEqual(rc, 4)
        self.assertEqual(calls["informulate"].call_count, 0)


_LANGPACK_SUBSTS = {
    **_WIN_SUBSTS,
    "L10NBASEDIR": "/l10n",
    "MOZ_PKG_LANGPACK_LOCALES_DIR": "browser/locales",
    "MOZ_PKG_LANGPACK_SUBDIRS": ["browser"],
    "MOZ_LANGPACK_EID_HOST": "firefox.mozilla.org",
    "MOZ_APP_VERSION": "156.0a1",
    "MOZ_APP_MAXVERSION": "156.*",
    "MOZ_APP_DISPLAYNAME": "Firefox Nightly",
    "PKG_LANGPACK_PATH": "xpi/",
}


class TestLangpack(unittest.TestCase):
    def _run(self, **kwargs):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = mozpath.normsep(tmpdir)
            os.makedirs(os.path.join(tmp, "dist"))
            with _patched(_LANGPACK_SUBSTS, tmp) as calls:
                rc = package_application.package_application(**kwargs)
        return rc, calls, tmp

    def _package_langpack_argv(self, tmp, ab_cd, metadata):
        return [
            f"--locale={ab_cd}",
            f"--xpi-stage={tmp}/dist/xpi-stage/locale-{ab_cd}",
            f"--metadata={metadata}",
            f"--output={tmp}/dist/xpi/firefox-156.0a1.{ab_cd}.langpack.xpi",
            f"--eid=langpack-{ab_cd}@firefox.mozilla.org",
            "--app-version=156.0a1",
            "--max-app-ver=156.*",
            "--app-name=Firefox Nightly",
            "--l10n-basedir=/l10n",
            "--include=chrome",
            "--include=localization",
            "--include=browser",
            "--include=manifest.json",
        ]

    def test_langpack(self):
        rc, calls, tmp = self._run(langpack=True)

        self.assertEqual(rc, 0)
        self.assertEqual(calls["l10n_merge"].call_count, 0)
        self.assertEqual(
            calls["l10n_stage"].call_args.args[0],
            [
                "--locale=en-US",
                f"--manifest={tmp}/l10n-manifest.json",
                f"--merge-tree={tmp}/browser/locales/merge-dir/en-US",
                f"--dest={tmp}/dist/xpi-stage/locale-en-US",
                "--topsrcdir=/src",
                f"--topobjdir={tmp}",
            ],
        )
        self.assertEqual(
            calls["package_langpack"].call_args.args[0],
            self._package_langpack_argv(
                tmp, "en-US", "/src/browser/locales/en-US/langpack-metadata.ftl"
            ),
        )

    def test_langpack_other_locale(self):
        rc, calls, tmp = self._run(langpack=True, ab_cd="de")

        self.assertEqual(rc, 0)
        self.assertEqual(
            calls["l10n_merge"].call_args.args[0],
            [
                "--locale=de",
                "--config=/src/browser/locales/l10n.toml",
                "--l10n-base=/l10n",
                f"--target={tmp}/browser/locales/merge-dir",
            ],
        )
        self.assertEqual(
            calls["l10n_stage"].call_args.args[0][2],
            f"--merge-tree={tmp}/browser/locales/merge-dir/de",
        )
        self.assertEqual(
            calls["package_langpack"].call_args.args[0],
            self._package_langpack_argv(
                tmp,
                "de",
                f"{tmp}/browser/locales/merge-dir/de/browser/langpack-metadata.ftl",
            ),
        )

    def test_langpack_off(self):
        _, calls, _ = self._run()

        self.assertEqual(calls["l10n_stage"].call_count, 0)
        self.assertEqual(calls["package_langpack"].call_count, 0)

    def test_langpack_stage_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "dist"))
            with _patched(_LANGPACK_SUBSTS, tmp) as calls:
                calls["l10n_stage"].return_value = 3
                rc = package_application.package_application(langpack=True)

        self.assertEqual(rc, 3)
        self.assertEqual(calls["package_langpack"].call_count, 0)


class TestStage(unittest.TestCase):
    def _stage(self, tmp, **kwargs):
        with mock.patch.object(
            package_application, "package_stage", return_value=0
        ) as staged:
            rc = package_application._stage(
                multilocale_txt=os.path.join(
                    tmp, "dist", "bin", "res", "multilocale.txt"
                ),
                locale_manifest_path=os.path.join(tmp, "locale-manifest.in"),
                base_path="@RESPATH@",
                source=os.path.join(tmp, "dist"),
                destination=os.path.join(tmp, "dist", "firefox"),
                **kwargs,
            )
        return rc, staged

    def test_locale_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, _ = self._stage(
                tmp, locales=["de", "fr"], locale_entries=["@RESPATH@/chrome/"]
            )

            self.assertEqual(rc, 0)
            multilocale = os.path.join(tmp, "dist", "bin", "res", "multilocale.txt")
            with open(multilocale, encoding="utf-8") as f:
                self.assertEqual(f.read(), "de,fr,en-US\n")
            with open(os.path.join(tmp, "locale-manifest.in"), encoding="utf-8") as f:
                self.assertEqual(
                    f.read(),
                    "\n".join([
                        "",
                        "[multilocale]",
                        "@RESPATH@/res/multilocale.txt",
                        "@RESPATH@/chrome/de@JAREXT@",
                        "@RESPATH@/chrome/de.manifest",
                        "@RESPATH@/chrome/fr@JAREXT@",
                        "@RESPATH@/chrome/fr.manifest",
                        "@RESPATH@/chrome/en-US@JAREXT@",
                        "@RESPATH@/chrome/en-US.manifest",
                        "",
                    ]),
                )

    def test_locale_manifest_define(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, staged = self._stage(
                tmp, locales=["de"], defines=["AB_CD=de"], minify=True
            )

            self.assertEqual(rc, 0)
            kwargs = staged.call_args.kwargs
            self.assertEqual(
                kwargs["defines"],
                [
                    "PKG_LOCALE_MANIFEST=" + os.path.join(tmp, "locale-manifest.in"),
                    "AB_CD=de",
                ],
            )
            self.assertEqual(kwargs["source"], os.path.join(tmp, "dist"))
            self.assertEqual(
                kwargs["destination"], os.path.join(tmp, "dist", "firefox")
            )
            self.assertTrue(kwargs["minify"])

    def test_return_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(
                package_application, "package_stage", return_value=2
            ):
                rc = package_application._stage(
                    multilocale_txt=os.path.join(tmp, "multilocale.txt"),
                    locale_manifest_path=os.path.join(tmp, "locale-manifest.in"),
                    base_path="@RESPATH@",
                    locales=["en-US"],
                    source=os.path.join(tmp, "dist"),
                    destination=os.path.join(tmp, "staged"),
                )

            self.assertEqual(rc, 2)


if __name__ == "__main__":
    mozunit.main()
